"""Config export (D23, P15-01): each part on its own or together, secrets only on request and encrypted."""

import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import AppHarness
from woosarr import admin as admin_cli
from woosarr.core import kv
from woosarr.core import secret_store as names
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordRole,
    Library,
    ManualGrant,
    MediaRequest,
    OidcIdentity,
    OidcProvider,
    PlexUser,
    ReactionPost,
    RoleLibrary,
)
from woosarr.services import config_transfer

# Assembled at runtime: no token-shaped literal in source.
PLEX_TOKEN = "plex-" + "tok" * 6
BOT_TOKEN = "bot-" + "secret" * 5
SEERR_KEY = "seerr-" + "key" * 8
OIDC_SECRET = "oidc-" + "client" * 4
PASSPHRASE = "correct horse " + "battery"


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        store = self.h.app.state.secrets
        with self.h.app.state.db.session() as s:
            kv.put(s, "plex.server", {"machine_id": "m1", "name": "Bragi"})
            kv.put(s, "discord.guild", {"id": "g1", "name": "Woos"})
            kv.put(s, "seerr.config", {"url": "http://seerr:5055", "version": "2.0"})
            s.add_all([PlexUser(plex_user_id="11", username="alice"), DiscordRole(role_id="801", name="Movie Night"),
                       Library(library_id="1", title="Movies", type="movie"),
                       Library(library_id="2", title="Kids", type="movie", is_exempt=True)])
            s.flush()
            provider = OidcProvider(slug="okta", name="Okta", kind="okta", issuer="https://x.okta.com",
                                    client_id="cid", enabled=True)
            s.add_all([RoleLibrary(role_id="801", library_id="1"),
                       ManualGrant(plex_user_id="11", library_id="2", granted_by="admin:admin"),
                       DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True,
                                   linked_by="self"),
                       ReactionPost(title="Pick", content="React!", channel_id="c1", message_id="m9",
                                    options=[{"emoji": "🎬", "role_id": "801", "label": "Movie Night"}]),
                       MediaRequest(seerr_request_id=7, discord_user_id="501", seerr_user_id=3, media_type="movie",
                                    tmdb_id=603, title="The Matrix", status="available"),
                       provider])
            s.flush()
            s.add(OidcIdentity(provider_id=provider.id, subject="sub-1", admin_user_id=1, email="a@example.com"))
            store.set(s, names.PLEX_TOKEN, PLEX_TOKEN)
            store.set(s, names.DISCORD_BOT_TOKEN, BOT_TOKEN)
            store.set(s, names.SEERR_API_KEY, SEERR_KEY)
            store.set(s, names.oidc_client_secret(provider.id), OIDC_SECRET)

    def tearDown(self):
        self.h.close()

    def export(self, **body):
        r = self.c.post("/api/v1/config/export", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_everything_by_default_without_secrets(self):
        r = self.export()
        self.assertIn('attachment; filename="woosarr-config-', r.headers["content-disposition"])
        doc = r.json()
        self.assertEqual((doc["kind"], doc["format"]), ("woosarr-config", 1))
        self.assertEqual(list(doc["parts"]), list(config_transfer.PARTS))
        self.assertEqual(doc["source"], {"plex_machine_id": "m1", "plex_server": "Bragi", "discord_guild_id": "g1",
                                         "discord_guild": "Woos"})
        self.assertIsNone(doc["secrets"])
        for secret in (PLEX_TOKEN, BOT_TOKEN, SEERR_KEY, OIDC_SECRET):
            self.assertNotIn(secret, r.text)
        self.assertNotIn("$argon2", r.text)  # no password hashes without secrets
        p = doc["parts"]
        self.assertEqual(p["settings"]["seerr"]["url"], "http://seerr:5055")
        self.assertIn("dry_run", p["settings"]["app"])
        self.assertEqual(p["libraries"]["role_mappings"],
                         [{"role_id": "801", "role_name": "Movie Night", "library_ids": ["1"]}])
        self.assertEqual({lib["title"]: lib["exempt"] for lib in p["libraries"]["libraries"]},
                         {"Movies": False, "Kids": True})
        self.assertEqual((p["manual_grants"][0]["plex_username"], p["manual_grants"][0]["library_id"]), ("alice", "2"))
        self.assertEqual((p["reaction_posts"][0]["message_id"], p["reaction_posts"][0]["options"][0]["role_id"]),
                         ("m9", "801"))
        self.assertEqual(p["sso"]["providers"][0]["slug"], "okta")
        self.assertEqual(p["sso"]["identities"], [{"provider": "okta", "subject": "sub-1", "admin": "admin",
                                                   "email": "a@example.com"}])
        self.assertEqual((p["links"][0]["discord_user_id"], p["links"][0]["verified"]), ("501", True))
        self.assertEqual([a["username"] for a in p["admins"]], ["admin"])
        self.assertEqual(p["requests"][0]["title"], "The Matrix")
        self.assertTrue(any(a["action"] == "first_admin_created" for a in p["audit"]))

    def test_each_part_on_its_own(self):
        for part in config_transfer.PARTS:
            with self.subTest(part=part):
                self.assertEqual(list(self.export(parts=[part]).json()["parts"]), [part])

    def test_secrets_are_encrypted_with_the_passphrase(self):
        r = self.export(parts=["settings", "sso", "admins"], include_secrets=True, passphrase=PASSPHRASE)
        for secret in (PLEX_TOKEN, BOT_TOKEN, SEERR_KEY, OIDC_SECRET):
            self.assertNotIn(secret, r.text)
        block = r.json()["secrets"]
        values = config_transfer.decrypt_secrets(block, PASSPHRASE)
        self.assertEqual((values["plex_token"], values["discord_bot_token"], values["seerr_api_key"],
                          values["oidc_client_secret:okta"]), (PLEX_TOKEN, BOT_TOKEN, SEERR_KEY, OIDC_SECRET))
        self.assertTrue(values["admin_password_hash:admin"].startswith("$argon2"))
        self.assertEqual(block["count"], len(values))
        with self.assertRaisesRegex(config_transfer.TransferError, "wrong passphrase"):
            config_transfer.decrypt_secrets(block, PASSPHRASE + "x")
        tampered = {**block, "ciphertext": block["ciphertext"][:-4] + "AAAA"}
        with self.assertRaises(config_transfer.TransferError):
            config_transfer.decrypt_secrets(tampered, PASSPHRASE)

    def test_sso_secrets_and_password_hashes_only_with_their_parts(self):
        block = self.export(parts=["settings"], include_secrets=True, passphrase=PASSPHRASE).json()["secrets"]
        self.assertEqual(sorted(config_transfer.decrypt_secrets(block, PASSPHRASE)),
                         ["discord_bot_token", "plex_token", "seerr_api_key"])

    def test_refusals(self):
        short = self.c.post("/api/v1/config/export", json={"include_secrets": True, "passphrase": "short"})
        self.assertEqual(short.status_code, 422)
        self.assertIn("at least 12", short.text)
        none = self.c.post("/api/v1/config/export", json={"include_secrets": True})
        self.assertEqual(none.status_code, 422)
        unknown = self.c.post("/api/v1/config/export", json={"parts": ["settings", "sessions"]})
        self.assertEqual(unknown.status_code, 422)
        self.assertIn("sessions", unknown.text)
        self.assertEqual(self.c.post("/api/v1/config/export", json={"parts": ["../etc"]}).status_code, 422)

    def test_read_only_api_keys_and_anonymous_callers_cannot_export(self):
        key = self.c.post("/api/v1/api-keys", json={"name": "diag", "scope": "read"}).json()["key"]
        reader = self.h.new_client()
        reader.headers["Authorization"] = f"Bearer {key}"
        self.assertEqual(reader.post("/api/v1/config/export", json={}).status_code, 403)
        self.assertEqual(reader.get("/api/v1/config/export/parts").status_code, 200)  # counts only
        self.assertEqual(self.h.new_client().post("/api/v1/config/export", json={}).status_code, 401)

    def test_part_counts(self):
        parts = {p["name"]: p["count"] for p in self.c.get("/api/v1/config/export/parts").json()["parts"]}
        self.assertEqual((parts["libraries"], parts["links"], parts["sso"], parts["requests"]), (2, 1, 1, 1))

    def test_export_is_audited_without_secrets(self):
        self.export(parts=["settings", "links"], include_secrets=True, passphrase=PASSPHRASE)
        with self.h.app.state.db.session() as s:
            entry = s.query(AuditLog).filter_by(action="config_exported").one()
            self.assertEqual((entry.actor, entry.details["parts"], entry.details["counts"], entry.details["secrets"]),
                             ("admin:admin", ["settings", "links"], {"links": 1}, 3))
            self.assertNotIn(PASSPHRASE, json.dumps(entry.details))

    def test_unreadable_secrets_are_named_and_left_out(self):
        with self.h.app.state.db.session() as s:
            from woosarr.db.models import Secret
            s.get(Secret, names.SEERR_API_KEY).ciphertext = "not-a-fernet-token"
        doc = self.export(parts=["settings"], include_secrets=True, passphrase=PASSPHRASE).json()
        self.assertEqual(doc["secrets_missing"], ["seerr_api_key"])
        self.assertNotIn("seerr_api_key", config_transfer.decrypt_secrets(doc["secrets"], PASSPHRASE))

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = admin_cli.main(list(argv), settings=self.h.settings)
        return code, out.getvalue()

    def test_console_export(self):
        path = Path(self.h.settings.config_dir) / "out.json"
        with mock.patch("getpass.getpass", side_effect=[PASSPHRASE, PASSPHRASE]):
            code, out = self.run_cli("export", "--parts", "settings,links", "--secrets", "-o", str(path))
        self.assertEqual(code, 0, out)
        self.assertIn("settings, links", out)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = json.loads(path.read_text())
        self.assertEqual(list(doc["parts"]), ["settings", "links"])
        self.assertEqual(config_transfer.decrypt_secrets(doc["secrets"], PASSPHRASE)["plex_token"], PLEX_TOKEN)
        with self.h.app.state.db.session() as s:
            self.assertEqual(s.query(AuditLog).filter_by(action="config_exported", actor="console").count(), 1)

    def test_console_export_refusals(self):
        with mock.patch("getpass.getpass", side_effect=[PASSPHRASE, PASSPHRASE + "x"]):
            self.assertEqual(self.run_cli("export", "--secrets")[0], 1)
        self.assertEqual(self.run_cli("export", "--parts", "nope")[0], 1)


if __name__ == "__main__":
    unittest.main()
