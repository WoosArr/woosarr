import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import plexapi
from cryptography.fernet import Fernet
from plexapi.exceptions import Unauthorized
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from tests.fake_plextv import MACHINE, FakePlexTV
from woosarr.core import app_settings, kv
from woosarr.core.secret_store import PLEX_TOKEN, SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import AuditLog, DiscordLink, DiscordMember, DiscordRole, PlexUser
from woosarr.db.session import Database, make_engine, migrate
from woosarr.linking import PlexIdentity
from woosarr.plex_api_gateway import PlexApiGateway, PlexAuthError
from woosarr.services import discord_bot, discord_cache, link_service
from woosarr.services.plex import K_SERVER, PlexService

OWNER = "owner-" + "tok" * 8
LINK = "link-" + "tok" * 8


class FakePin:
    instances: ClassVar[list] = []

    def __init__(self, oauth=False, headers=None):
        self.oauth, self.headers = oauth, headers
        self.token, self.expired, self._polls = None, False, 0
        FakePin.instances.append(self)

    def oauthUrl(self, forward=None):  # plexapi's method name
        return f"https://app.plex.tv/auth/#!?code=abc&forwardUrl={forward}"

    def checkLogin(self):
        self._polls += 1
        return self.token is not None


class FakeAccounts:
    """account_factory(token=...) with signout tracking."""

    def __init__(self, fake_tv):
        self.tv = fake_tv
        self.valid = {OWNER: ("1", "owner"), LINK: ("11", "alice")}
        self.signed_out = []
        self.keep_alive_after_signout = False

    def __call__(self, token):
        if token not in self.valid:
            raise Unauthorized("(401)")
        uid, name = self.valid[token]

        def signout():
            self.signed_out.append(token)
            if not self.keep_alive_after_signout:
                self.valid.pop(token, None)

        acct = self.tv.account()
        acct.id, acct.username, acct.title, acct.email = uid, name, name, None
        acct.signout = signout
        acct.resources = lambda: [
            SimpleNamespace(provides="server", clientIdentifier=MACHINE, name="Home", owned=True, presence=True,
                            productVersion="1.41", platform="Linux"),
            SimpleNamespace(provides="server", clientIdentifier="friend-srv", name="Friend's", owned=False,
                            presence=True, productVersion="1.40", platform="Windows"),
            SimpleNamespace(provides="client,player", clientIdentifier="tv", name="TV", owned=True, presence=True,
                            productVersion="", platform=""),
        ]
        return acct


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.secrets = SecretStore(SecretBox(Fernet.generate_key()))
        self.tv = FakePlexTV()
        self.accounts = FakeAccounts(self.tv)
        FakePin.instances = []
        self.plex = PlexService(self.db, self.secrets, pin_factory=FakePin, account_factory=self.accounts)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()


class PlexServiceTests(Base):
    def test_stable_client_identifier_applied_to_plexapi(self):
        again = PlexService(self.db, self.secrets, pin_factory=FakePin, account_factory=self.accounts)
        self.assertEqual(again.client_id, self.plex.client_id)
        self.assertEqual(plexapi.BASE_HEADERS["X-Plex-Client-Identifier"], self.plex.client_id)
        self.assertEqual(plexapi.BASE_HEADERS["X-Plex-Product"], "WooSarr")

    def test_owner_sign_in_flow(self):
        self.assertEqual(self.plex.status()["detail"], "Not signed in to Plex")
        pin_id, url = self.plex.start_login("http://host/setup")
        self.assertIn("forwardUrl=http://host/setup", url)
        self.assertEqual(self.plex.poll_token(pin_id), ("pending", None))
        FakePin.instances[-1].token = OWNER
        state, token = self.plex.poll_token(pin_id)
        self.assertEqual(state, "done")
        self.assertEqual(self.plex.poll_token(pin_id)[0], "expired")  # one-shot
        info = self.plex.complete_owner_login(token)
        self.assertEqual(info["username"], "owner")
        with self.db.session() as s:
            self.assertEqual(self.secrets.get(s, PLEX_TOKEN), OWNER)
        self.assertEqual(self.plex.status()["detail"], "Signed in; choose a server")
        self.assertIsNone(self.plex.gateway())

    def test_server_choice_requires_an_owned_server(self):
        self.plex.complete_owner_login(OWNER)
        servers = {s["machine_id"]: s for s in self.plex.list_servers()}
        self.assertEqual(set(servers), {MACHINE})  # players and servers shared with us left out
        with self.assertRaises(ValueError):
            self.plex.select_server("friend-srv")
        self.plex.select_server(MACHINE)
        self.assertIsInstance(self.plex.gateway(), PlexApiGateway)
        st = self.plex.status()
        self.assertTrue(st["configured"])
        self.assertTrue(st["connected"])

    def test_disconnect_signs_out_and_forgets(self):
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        self.plex.disconnect()
        self.assertIn(OWNER, self.accounts.signed_out)
        self.assertIsNone(self.plex.gateway())
        with self.db.session() as s:
            self.assertIsNone(kv.get(s, K_SERVER))

    def test_link_identity_token_is_invalidated_and_verified(self):
        self.plex.start_login(None, "link", "woosarr-link-xyz")
        self.assertEqual(FakePin.instances[-1].headers, {"X-Plex-Client-Identifier": "woosarr-link-xyz"})
        identity = self.plex.capture_link_identity(LINK)
        self.assertEqual(identity, PlexIdentity("11", "alice"))
        self.assertIn(LINK, self.accounts.signed_out)
        with self.assertRaises(Unauthorized):
            self.accounts(token=LINK)

    def test_link_fails_if_token_survives_signout(self):
        self.accounts.keep_alive_after_signout = True
        with self.assertRaises(RuntimeError):
            self.plex.capture_link_identity(LINK)

    def test_owner_token_never_persisted_for_links(self):
        self.plex.capture_link_identity(LINK)
        with self.db.session() as s:
            self.assertIsNone(self.secrets.get(s, PLEX_TOKEN))

    def test_read_and_write_checks(self):
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        self.tv.shares["11"] = {"id": 7, "sections": {101}}
        report = self.plex.read_check()
        self.assertEqual(report["users"], 4)  # incl. the owner
        self.assertEqual(report["errors"], [])
        result = self.plex.write_check("11")
        self.assertTrue(result["passed"], result)
        self.assertEqual([s["step"] for s in result["steps"]], ["rewrite same set", "add one library", "restore original"])
        self.assertEqual(self.tv.shares["11"]["sections"], {101})  # restored

    def test_write_check_detects_merge_semantics(self):
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        self.tv.shares["11"] = {"id": 7, "sections": {101, 102}}
        real_query = self.tv.query

        def merging(url, method="get", json=None, **kw):
            if method == "put":  # a Plex that merged instead of replacing
                owner = next(u for u, s in self.tv.shares.items() if url.endswith(f"/{s['id']}"))
                self.tv.shares[owner]["sections"] |= set(json["shared_server"]["library_section_ids"])
                return None
            return real_query(url, method, json=json, **kw)

        self.tv.query = merging
        result = self.plex.write_check("11")
        self.assertFalse(result["passed"])

    def test_not_signed_in_errors(self):
        with self.assertRaises(PlexAuthError):
            self.plex.read_check()
        with self.assertRaises(PlexAuthError):
            self.plex.list_servers()


class LinkServiceTests(Base):
    def setUp(self):
        super().setUp()
        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="11", username="Alice", email="alice@x.com"),
                       PlexUser(plex_user_id="12", username="bob", email="bob@x.com")])
            app_settings.update(s, {"link_cooldown_seconds": 0})

    def link(self, fn, *args):
        with self.db.session() as s:
            return fn(s, *args)

    def links(self):
        with self.db.session() as s:
            return {(row.plex_user_id, row.discord_user_id, row.verified) for row in s.scalars(select(DiscordLink))}

    def test_username_and_email_match_case_insensitively(self):
        self.assertTrue(self.link(link_service.link_by_username, "d1", "  alice ").ok)
        self.assertTrue(self.link(link_service.link_by_username, "d2", "BOB@x.com").ok)
        self.assertEqual(self.links(), {("11", "d1", False), ("12", "d2", False)})

    def test_unknown_user(self):
        r = self.link(link_service.link_by_username, "d1", "mallory")
        self.assertFalse(r.ok)
        self.assertIn("No Plex user", r.message)

    def test_one_to_one_and_verified_supersedes(self):
        self.link(link_service.link_by_username, "d1", "alice")
        taken = self.link(link_service.link_by_username, "d2", "alice")
        self.assertFalse(taken.ok)
        self.assertIn("already linked to someone else", taken.message)
        r = self.link(link_service.link_by_identity, "d2", PlexIdentity("11", "Alice"))
        self.assertTrue(r.ok)
        self.assertEqual(self.links(), {("11", "d2", True)})
        again = self.link(link_service.link_by_username, "d2", "bob")
        self.assertFalse(again.ok)
        self.assertIn("already linked to another Plex account", again.message)

    def test_upgrade_own_link(self):
        self.link(link_service.link_by_username, "d1", "alice")
        r = self.link(link_service.link_by_identity, "d1", PlexIdentity("11", "Alice"))
        self.assertTrue(r.ok)
        self.assertEqual(self.links(), {("11", "d1", True)})

    def test_signed_in_account_without_server_access(self):
        r = self.link(link_service.link_by_identity, "d1", PlexIdentity("99", "stranger"))
        self.assertFalse(r.ok)
        self.assertIn("doesn't have access", r.message)

    def test_username_link_limited_to_roles(self):
        with self.db.session() as s:
            s.add_all([DiscordMember(discord_user_id="d1", username="a", role_ids=["r-trusted"]),
                       DiscordMember(discord_user_id="d2", username="b", role_ids=["r-other"])])
            app_settings.update(s, {"username_link_all_members": False, "username_link_role_ids": ["r-trusted"]})
        self.assertTrue(self.link(link_service.username_link_allowed, "d1"))
        self.assertFalse(self.link(link_service.username_link_allowed, "d2"))
        self.assertFalse(self.link(link_service.username_link_allowed, "d3"))  # not in the member cache
        self.assertTrue(self.link(link_service.link_by_username, "d1", "alice").ok)
        refused = self.link(link_service.link_by_username, "d2", "bob")
        self.assertFalse(refused.ok)
        self.assertIn("Sign in with Plex", refused.message)
        # Plex sign-in (verified) is never limited.
        self.assertTrue(self.link(link_service.link_by_identity, "d2", PlexIdentity("12", "bob")).ok)
        self.assertEqual(self.links(), {("11", "d1", False), ("12", "d2", True)})

    def test_cooldown_and_audit(self):
        with self.db.session() as s:
            app_settings.update(s, {"link_cooldown_seconds": 300})
        self.link(link_service.link_by_username, "d1", "nobody")
        self.assertGreater(self.link(link_service.cooldown_remaining, "d1"), 250)
        self.assertEqual(self.link(link_service.cooldown_remaining, "d2"), 0)
        with self.db.session() as s:
            actions = [a.action for a in s.scalars(select(AuditLog))]
        self.assertIn("link_failed", actions)


class DiscordCacheTests(Base):
    def setUp(self):
        super().setUp()
        with self.db.session() as s:
            s.add(PlexUser(plex_user_id="11", username="alice"))
            s.flush()
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d1", method="oauth", verified=True, linked_by="self"))

    def run_db(self, fn, *args):
        with self.db.session() as s:
            return fn(s, *args)

    def test_full_sync_marks_missing(self):
        roles = [discord_cache.RoleData("r1", "Movies"), discord_cache.RoleData("r2", "TV")]
        members = [discord_cache.MemberData("d1", "alice", "Alice", ("r1",)),
                   discord_cache.MemberData("d2", "bob", None, ())]
        self.run_db(discord_cache.full_sync, roles, members)
        self.run_db(discord_cache.full_sync, roles[:1], members[:1])
        with self.db.session() as s:
            self.assertTrue(s.get(DiscordRole, "r2").deleted)
            self.assertFalse(s.get(DiscordMember, "d2").in_guild)

    def test_member_changes_report_linked_plex_user_only_when_changed(self):
        m = discord_cache.MemberData("d1", "alice", "Alice", ("r1",))
        self.assertEqual(self.run_db(discord_cache.member_changed, m), "11")
        self.assertIsNone(self.run_db(discord_cache.member_changed, m))
        m2 = discord_cache.MemberData("d1", "alice", "Alice", ("r1", "r2"))
        self.assertEqual(self.run_db(discord_cache.member_changed, m2), "11")
        unlinked = discord_cache.MemberData("d9", "x", None, ("r1",))
        self.assertIsNone(self.run_db(discord_cache.member_changed, unlinked))
        self.assertEqual(self.run_db(discord_cache.member_left, "d1"), "11")
        self.assertIsNone(self.run_db(discord_cache.member_left, "d1"))

    def test_role_and_member_writes_survive_a_concurrent_insert(self):
        """Live 2026-10-18: an integration (Patreon) created a role and Discord sent role-create
        and role-update together; both handlers saw "no row" and inserted -> UNIQUE constraint."""
        self.run_db(discord_cache.upsert_role, discord_cache.RoleData("r7", "Patreon", managed=True))
        self.run_db(discord_cache.member_changed, discord_cache.MemberData("d1", "alice", None, ()))
        real_get = SASession.get

        def stale_get(session, model, key, *a, **kw):  # the other handler hasn't committed yet
            return None if model in (DiscordRole, DiscordMember) else real_get(session, model, key, *a, **kw)

        with mock.patch.object(SASession, "get", stale_get):
            self.run_db(discord_cache.upsert_role, discord_cache.RoleData("r7", "Patreon Gold", position=3,
                                                                           managed=True))
            self.run_db(discord_cache.member_changed, discord_cache.MemberData("d1", "alice", "Al", ("r7",)))
        with self.db.session() as s:
            role = s.get(DiscordRole, "r7")
            self.assertEqual((role.name, role.position, role.deleted), ("Patreon Gold", 3, False))
            self.assertEqual((s.get(DiscordMember, "d1").role_ids, s.get(DiscordMember, "d1").display_name),
                             (["r7"], "Al"))

    def test_concurrent_role_events_for_a_new_role(self):
        errors = []

        def hit(i):
            try:
                self.run_db(discord_cache.upsert_role, discord_cache.RoleData("r9", f"name {i}"))
                self.run_db(discord_cache.member_changed, discord_cache.MemberData("d5", "x", None, (f"r{i}",)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with self.db.session() as s:
            self.assertEqual(len(s.scalars(select(DiscordRole).where(DiscordRole.role_id == "r9")).all()), 1)

    def test_role_deleted_strips_role_and_returns_holders(self):
        self.run_db(discord_cache.upsert_role, discord_cache.RoleData("r1", "Movies"))
        self.run_db(discord_cache.member_changed, discord_cache.MemberData("d1", "alice", None, ("r1",)))
        self.assertEqual(self.run_db(discord_cache.role_deleted, "r1"), ["11"])
        with self.db.session() as s:
            self.assertEqual(s.get(DiscordMember, "d1").role_ids, [])


class DiscordServiceTests(Base):
    def test_invite_url(self):
        url = discord_bot.invite_url("123456789012345678")
        self.assertIn("client_id=123456789012345678", url)
        self.assertIn("applications.commands", url)

    def test_token_without_members_intent_is_refused(self):
        svc = discord_bot.DiscordService(self.db, self.secrets, lambda *a, **k: None, self.plex)
        info = discord_bot.BotInfo("1", "bot", "2", members_intent=False, invite_url="x")
        with (
            mock.patch.object(discord_bot, "validate_token", mock.AsyncMock(return_value=info)),
            self.assertRaises(discord_bot.DiscordConfigError) as cm,
        ):
            asyncio.run(svc.set_token("t" * 60))
        self.assertIn("Server Members Intent", str(cm.exception))
        with self.db.session() as s:
            self.assertFalse(self.secrets.is_set(s, "discord.bot_token"))

    def test_status_before_setup(self):
        svc = discord_bot.DiscordService(self.db, self.secrets, lambda *a, **k: None, self.plex)
        st = svc.status()
        self.assertFalse(st["configured"])
        self.assertEqual(st["detail"], "Bot token not set")
        self.assertFalse(st["connecting"])

    def test_status_connecting_until_ready_or_error(self):
        svc = discord_bot.DiscordService(self.db, self.secrets, lambda *a, **k: None, self.plex)
        with (
            mock.patch.object(svc, "_token", return_value="t"),
            mock.patch.object(svc, "config", return_value=({"application_id": "1"}, {"id": "2", "name": "g"})),
        ):
            st = svc.status()
            self.assertEqual((st["connected"], st["connecting"], st["detail"]), (False, True, "Connecting…"))
            svc._error = "Discord refused the bot token"
            st = svc.status()
            self.assertEqual((st["connected"], st["connecting"]), (False, False))

    def test_validate_token_rejects_garbage_without_network(self):
        with self.assertRaises(discord_bot.DiscordConfigError):
            asyncio.run(discord_bot.validate_token("not a token"))
