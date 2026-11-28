"""Config import (D23, P15-02): preview writes nothing, apply moves the chosen parts, ids are matched to
this server, secrets need the passphrase, and hostile files are refused."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import AppHarness
from woosarr import admin as admin_cli
from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.db.models import (
    AdminUser,
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
from woosarr.services import config_import

PLEX_TOKEN = "plex-" + "tok" * 6
BOT_TOKEN = "bot-" + "secret" * 5
OIDC_SECRET = "oidc-" + "client" * 4
PASSPHRASE = "correct horse " + "battery"


def source_app() -> AppHarness:
    h = AppHarness()
    h.setup_admin()
    store = h.app.state.secrets
    with h.app.state.db.session() as s:
        kv.put(s, "plex.server", {"machine_id": "m1", "name": "Bragi"})
        kv.put(s, "plex.account", {"id": "1", "username": "owner"})
        kv.put(s, "discord.guild", {"id": "111", "name": "Woos"})
        kv.put(s, "discord.bot", {"id": "222", "username": "WooSarr"})
        app_settings.update(s, {"dry_run": False, "reconcile_interval_minutes": 90})
        s.add_all([PlexUser(plex_user_id="11", username="alice"), DiscordRole(role_id="801", name="Movie Night"),
                   DiscordRole(role_id="802", name="Kids Club"),
                   Library(library_id="1", title="Movies", type="movie"),
                   Library(library_id="2", title="Kids", type="movie", is_exempt=True),
                   Library(library_id="3", title="Anime", type="show")])
        s.flush()
        provider = OidcProvider(slug="okta", name="Okta", kind="okta", issuer="https://x.okta.com", client_id="cid",
                                enabled=True)
        s.add_all([RoleLibrary(role_id="801", library_id="1"), RoleLibrary(role_id="801", library_id="3"),
                   RoleLibrary(role_id="802", library_id="2"),
                   ManualGrant(plex_user_id="11", library_id="3", granted_by="admin:admin"),
                   DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True, linked_by="self"),
                   ReactionPost(title="Pick", content="React!", channel_id="333", message_id="444",
                                options=[{"emoji": "🎬", "role_id": "801", "label": "Movie Night"},
                                         {"emoji": "🧸", "role_id": "802", "label": "Kids Club"}]),
                   MediaRequest(seerr_request_id=7, discord_user_id="501", seerr_user_id=3, media_type="movie",
                                tmdb_id=603, title="The Matrix", status="available"),
                   AdminUser(username="bob", password_hash="$argon2id$v=19$m=1,t=1,p=1$c2FsdA$aGFzaA"),
                   provider])
        s.flush()
        s.add(OidcIdentity(provider_id=provider.id, subject="sub-1", admin_user_id=1))
        store.set(s, names.PLEX_TOKEN, PLEX_TOKEN)
        store.set(s, names.DISCORD_BOT_TOKEN, BOT_TOKEN)
        store.set(s, names.oidc_client_secret(provider.id), OIDC_SECRET)
    return h


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = source_app()
        try:
            r = src.client.post("/api/v1/config/export", json={"include_secrets": True, "passphrase": PASSPHRASE})
            assert r.status_code == 200, r.text
            cls.doc = r.json()
        finally:
            src.close()

    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        # Imported bot tokens restart the bot; never reach Discord from a test.
        self.h.app.state.discord.restart = mock.AsyncMock()

    def tearDown(self):
        self.h.close()

    def db(self):
        return self.h.app.state.db.session()

    def post(self, path, **body):
        return self.c.post(f"/api/v1/config/import{path}", json={"file": self.doc, **body})

    def apply(self, **body):
        r = self.post("", **body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_preview_writes_nothing(self):
        r = self.post("/preview", passphrase=PASSPHRASE)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["available"], [*["settings", "libraries", "manual_grants", "reaction_posts", "sso", "links",
                                            "admins", "requests", "audit"], "secrets"])
        self.assertEqual(p["source"]["plex_server"], "Bragi")
        self.assertTrue(p["secrets_readable"])
        self.assertIn("Plex token", p["secrets_found"])
        self.assertTrue(any("Plex isn't connected here yet" in w for w in p["warnings"]))
        with self.db() as s:
            self.assertEqual((s.query(Library).count(), s.query(DiscordLink).count(), s.query(ReactionPost).count()),
                             (0, 0, 0))
            self.assertFalse(self.h.app.state.secrets.is_set(s, names.PLEX_TOKEN))
            self.assertEqual(s.query(AuditLog).filter_by(action="config_imported").count(), 0)

    def test_full_import_into_a_fresh_server(self):
        result = self.apply(passphrase=PASSPHRASE)
        self.assertEqual(sorted(result["reconnecting"]), ["discord", "plex"])
        self.h.app.state.discord.restart.assert_awaited_once()
        with self.db() as s:
            self.assertEqual({lib.library_id: lib.is_exempt for lib in s.query(Library)}, {"1": False, "2": True, "3": False})
            self.assertEqual(sorted((m.role_id, m.library_id) for m in s.query(RoleLibrary)),
                             [("801", "1"), ("801", "3"), ("802", "2")])
            self.assertEqual(s.get(DiscordRole, "801").name, "Movie Night")  # until Discord connects
            self.assertEqual([(g.plex_user_id, g.library_id) for g in s.query(ManualGrant)], [("11", "3")])
            self.assertEqual(s.get(PlexUser, "11").username, "alice")
            self.assertEqual([(lk.discord_user_id, lk.verified) for lk in s.query(DiscordLink)], [("501", True)])
            post = s.query(ReactionPost).one()
            self.assertEqual((post.message_id, post.channel_id, len(post.options)), ("444", "333", 2))
            provider = s.query(OidcProvider).one()
            self.assertTrue(provider.enabled)
            self.assertEqual(self.h.app.state.secrets.get(s, names.oidc_client_secret(provider.id)), OIDC_SECRET)
            self.assertEqual(s.query(OidcIdentity).one().admin_user_id, s.query(AdminUser).filter_by(username="admin").one().id)
            bob = s.query(AdminUser).filter_by(username="bob").one()
            self.assertTrue(bob.password_hash.startswith("$argon2"))
            self.assertEqual(s.query(MediaRequest).one().title, "The Matrix")
            self.assertTrue(s.query(AuditLog).filter_by(action="plex_signed_in").count() == 0)
            self.assertGreaterEqual(s.query(AuditLog).filter_by(action="first_admin_created").count(), 2)
            st = app_settings.load(s)
            self.assertEqual((st.reconcile_interval_minutes, st.dry_run), (90, True))  # dry-run forced on
            self.assertEqual(self.h.app.state.secrets.get(s, names.PLEX_TOKEN), PLEX_TOKEN)
            self.assertEqual(kv.get(s, "plex.server")["machine_id"], "m1")
            self.assertEqual(kv.get(s, "discord.guild")["id"], "111")
            entry = s.query(AuditLog).filter_by(action="config_imported").one()
            self.assertEqual((entry.actor, entry.details["source_plex"]), ("admin:admin", "Bragi"))
            self.assertNotIn(PLEX_TOKEN, json.dumps(entry.details))

    def test_each_part_on_its_own(self):
        for part in [*config_import.IMPORT_PARTS]:
            with self.subTest(part=part):
                h = AppHarness()
                try:
                    c = h.setup_admin()
                    h.app.state.discord.restart = mock.AsyncMock()
                    r = c.post("/api/v1/config/import", json={"file": self.doc, "parts": [part], "passphrase": PASSPHRASE})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual([p["name"] for p in r.json()["parts"]], [part])
                finally:
                    h.close()

    def test_without_the_secrets_part_tokens_stay_out(self):
        parts = [p for p in config_import.IMPORT_PARTS if p != "secrets"]
        result = self.apply(parts=parts, passphrase=PASSPHRASE)
        self.assertEqual(result["reconnecting"], [])
        self.h.app.state.discord.restart.assert_not_awaited()
        with self.db() as s:
            self.assertFalse(self.h.app.state.secrets.is_set(s, names.PLEX_TOKEN))
            self.assertIsNone(s.query(AdminUser).filter_by(username="bob").one().password_hash)
            self.assertFalse(s.query(OidcProvider).one().enabled)  # no client secret: imported switched off
        self.assertTrue(any("comes without a password" in w for w in result["warnings"]))

    def test_another_plex_server_matches_libraries_by_title(self):
        with self.db() as s:
            kv.put(s, "plex.server", {"machine_id": "m2", "name": "Odin"})
            s.add_all([Library(library_id="9", title="movies", type="movie"), Library(library_id="8", title="Kids", type="movie")])
        result = self.apply(parts=["libraries", "manual_grants"])
        with self.db() as s:
            self.assertEqual(sorted((m.role_id, m.library_id) for m in s.query(RoleLibrary)), [("801", "9"), ("802", "8")])
            self.assertTrue(s.get(Library, "8").is_exempt)
            self.assertEqual(s.query(ManualGrant).count(), 0)  # Anime isn't on Odin
        self.assertTrue(any("not found on this Plex server" in w and "Anime" in w for w in result["warnings"]))

    def test_another_discord_server_matches_roles_by_name_and_unpublishes_posts(self):
        with self.db() as s:
            kv.put(s, "discord.guild", {"id": "999", "name": "Other"})
            s.add(DiscordRole(role_id="901", name="movie night"))
        result = self.apply(parts=["libraries", "reaction_posts", "settings"])
        with self.db() as s:
            self.assertEqual(sorted((m.role_id, m.library_id) for m in s.query(RoleLibrary)), [("901", "1"), ("901", "3")])
            post = s.query(ReactionPost).one()
            self.assertEqual((post.message_id, post.channel_id, [o["role_id"] for o in post.options]), (None, None, ["901"]))
        warnings = " ".join(result["warnings"])
        self.assertIn("Kids Club", warnings)
        self.assertIn("imported unpublished", warnings)
        self.assertIn("point at Woos", warnings)

    def test_wrong_passphrase(self):
        p = self.post("/preview", passphrase=PASSPHRASE + "x").json()
        self.assertFalse(p["secrets_readable"])
        self.assertTrue(any("wrong passphrase" in w for w in p["warnings"]))  # fixed text
        r = self.post("", passphrase=PASSPHRASE + "x")
        self.assertEqual(r.status_code, 422)
        with self.db() as s:
            self.assertEqual(s.query(Library).count(), 0)  # nothing half-imported
        self.assertEqual(self.post("", parts=["settings"], passphrase=PASSPHRASE + "x").status_code, 200)

    def test_importing_twice_does_not_duplicate_audit_entries(self):
        self.apply(parts=["audit"])
        with self.db() as s:
            first = s.query(AuditLog).count()
        self.apply(parts=["audit"])
        with self.db() as s:
            self.assertEqual(s.query(AuditLog).count(), first + 1)  # only the second config_imported entry

    def test_the_importing_admin_is_never_switched_off(self):
        doc = json.loads(json.dumps(self.doc))
        doc["parts"]["admins"] = [{"username": "admin", "auth_source": "local", "active": False}]
        r = self.c.post("/api/v1/config/import", json={"file": doc, "parts": ["admins"]})
        self.assertEqual(r.status_code, 200, r.text)
        with self.db() as s:
            self.assertTrue(s.query(AdminUser).filter_by(username="admin").one().is_active)

    def test_refusals_and_hostile_files(self):
        def bad(doc, *expect):
            r = self.c.post("/api/v1/config/import/preview", json={"file": doc})
            self.assertEqual(r.status_code, 422, r.text)
            for text in expect:
                self.assertIn(text, r.text)

        bad({"kind": "something"}, "isn't a WooSarr export")
        bad(self.doc | {"format": 99, "woosarr": "9.0.0"}, "newer WooSarr", "9.0.0")
        bad(self.doc | {"parts": {"sessions": []}}, "unknown part")
        broken = json.loads(json.dumps(self.doc))
        broken["parts"]["libraries"]["libraries"][0]["id"] = "../../etc"
        bad(broken, "damaged or was edited")
        broken = json.loads(json.dumps(self.doc))
        broken["parts"]["links"][0]["method"] = "magic"
        bad(broken, "links")
        broken = json.loads(json.dumps(self.doc))
        broken["parts"]["sso"]["providers"][0]["issuer"] = "javascript:alert(1)"
        bad(broken)
        r = self.c.post("/api/v1/config/import/preview", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/api/v1/config/import/preview", content=b"{}",
                        headers={"Content-Type": "application/json", "Content-Length": str(config_import.MAX_BYTES + 1)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.post("", parts=["nope"]).status_code, 422)

    def test_read_only_keys_and_anonymous_callers_cannot_import(self):
        key = self.c.post("/api/v1/api-keys", json={"name": "diag", "scope": "read"}).json()["key"]
        reader = self.h.new_client()
        reader.headers["Authorization"] = f"Bearer {key}"
        self.assertEqual(reader.post("/api/v1/config/import/preview", json={"file": self.doc}).status_code, 403)
        self.assertEqual(self.h.new_client().post("/api/v1/config/import", json={"file": self.doc}).status_code, 401)

    def test_console_import(self):
        path = Path(self.h.settings.config_dir) / "in.json"
        path.write_text(json.dumps(self.doc))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("getpass.getpass", return_value=PASSPHRASE):
            code = admin_cli.main(["import", str(path), "--secrets", "--yes"], settings=self.h.settings)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("dry-run is on", err.getvalue())
        with self.db() as s:
            self.assertEqual(s.query(DiscordLink).count(), 1)
            self.assertEqual(self.h.app.state.secrets.get(s, names.PLEX_TOKEN), PLEX_TOKEN)
            self.assertEqual(s.query(AuditLog).filter_by(action="config_imported", actor="console").count(), 1)

    def test_console_import_from_stdin_needs_yes(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin_cli.main(["import", "-"], settings=self.h.settings), 1)


if __name__ == "__main__":
    unittest.main()
