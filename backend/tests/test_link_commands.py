"""/link, /unlink and the username form answer Discord before any database work (3 s limit)."""

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace

import discord
from cryptography.fernet import Fernet

from tests.helpers import AppHarness
from tests.test_requests import FakeInteraction
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.services.discord_bot import DiscordService


class LinkCommandTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.svc = DiscordService(self.h.app.state.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda *a, **k: None, plex=None)

    def tearDown(self):
        self.h.close()

    def test_link_defers_first_then_offers_the_buttons(self):
        it = FakeInteraction(501)
        asyncio.run(self.svc.handle_link_command(it))
        self.assertEqual(it.log[0], ("defer", {"ephemeral": True, "thinking": True}))
        kind, text, kw = it.log[1]
        self.assertEqual(kind, "followup")
        self.assertIn("Sign in with Plex", text)
        self.assertTrue(kw["ephemeral"])
        self.assertIsNotNone(kw["view"])

    def test_unlink_when_not_linked(self):
        it = FakeInteraction(501)
        asyncio.run(self.svc.handle_unlink_command(it))
        self.assertEqual([e[0] for e in it.log], ["defer", "followup"])
        self.assertIn("isn't linked", it.log[1][1])

    def test_too_late_is_logged_and_the_member_is_told_by_dm(self):
        from unittest import mock

        from woosarr.services import discord_timing
        discord_timing._too_late_dms.clear()
        it = FakeInteraction(501)
        it.user.send = mock.AsyncMock()
        it.created_at = discord.utils.utcnow() - timedelta(seconds=4)

        async def gone(**kw):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                                   {"code": 10062, "message": "Unknown interaction"})

        it.response.defer = gone
        with self.assertLogs("woosarr.discord", "WARNING") as logs:
            asyncio.run(self.svc.handle_link_command(it))
        self.assertIn("/link from member 501 answered too late: Discord (or the network to it) delivered it late. "
                      "It reached WooSarr 4.", logs.output[0])
        self.assertEqual(it.log, [])  # nothing else was tried
        self.assertIn("try /link again", it.user.send.await_args.args[0])
        asyncio.run(self.svc.handle_link_command(it))
        self.assertEqual(it.user.send.await_count, 1)  # at most one DM a minute


class AdminLinkCommandTests(unittest.TestCase):
    """`/link member plex_user`: admins link a member to a Plex user (verified), like Users → Link."""

    def setUp(self):
        from woosarr.db.models import PlexUser
        self.h = AppHarness()
        self.db = self.h.app.state.db
        self.triggered = []
        self.svc = DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda ids, reason, **k: self.triggered.append((ids, reason)), plex=None)
        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="p1", username="biggs_plex", email="biggs@example.com"),
                       PlexUser(plex_user_id="p2", username="wedge", email="wedge@example.com"),
                       PlexUser(plex_user_id="p0", username="owner", is_owner=True)])

    def tearDown(self):
        self.h.close()

    @staticmethod
    def member(uid):
        return SimpleNamespace(id=uid, mention=f"<@{uid}>", bot=False)

    def link(self, it, member=None, plex_user=None):
        asyncio.run(self.svc.handle_link_command(it, member, plex_user))
        return it.log[-1][1]

    def test_admin_links_a_member_verified(self):
        from sqlalchemy import select

        from woosarr.db.models import AuditLog, DiscordLink
        text = self.link(FakeInteraction(900, admin=True), self.member(777), "p1")
        self.assertEqual(text, "Linked <@777> to Plex account **biggs\\_plex** (verified). Their library access "
                               "updates shortly.")
        with self.db.session() as s:
            link = s.scalars(select(DiscordLink)).one()
            self.assertEqual((link.plex_user_id, link.discord_user_id, link.method, link.verified, link.linked_by),
                             ("p1", "777", "admin", True, "discord-admin:900"))
            entry = s.scalars(select(AuditLog).where(AuditLog.action == "link_admin")).one()
            self.assertEqual((entry.actor.split(" ")[0], entry.target_id, entry.details["via"]),
                             ("discord:900", "p1", "discord"))
        self.assertEqual(self.triggered, [(["p1"], "linked by discord admin 900")])
        # A typed username works too, and adds a second member to the account (a family).
        text = self.link(FakeInteraction(900, admin=True), self.member(778), "BIGGS_PLEX")
        self.assertIn("They share it with 1 other member.", text)

    def test_member_already_linked_elsewhere_and_unknown_plex_user(self):
        self.link(FakeInteraction(900, admin=True), self.member(777), "p1")
        self.assertIn("already linked to another Plex account", self.link(FakeInteraction(900, admin=True),
                                                                           self.member(777), "p2"))
        self.assertIn("No Plex user with that name", self.link(FakeInteraction(900, admin=True),
                                                               self.member(779), "nobody"))
        self.assertIn("Pick the Plex user", self.link(FakeInteraction(900, admin=True), self.member(779)))

    def test_admin_without_member_links_themselves(self):
        self.assertIn("Linked <@900>", self.link(FakeInteraction(900, admin=True), None, "wedge"))

    def test_members_who_arent_admins_cant_link_others_or_list_plex_users(self):
        from sqlalchemy import func, select

        from woosarr.db.models import DiscordLink
        self.assertIn("Only admins can link other members", self.link(FakeInteraction(501), self.member(777), "p1"))
        with self.db.session() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(DiscordLink)), 0)
        self.assertEqual(asyncio.run(self.svc.plex_user_suggestions(FakeInteraction(501), "")), [])

    def test_suggestions_show_usernames_and_who_is_linked_never_emails(self):
        self.link(FakeInteraction(900, admin=True), self.member(777), "p1")
        found = asyncio.run(self.svc.plex_user_suggestions(FakeInteraction(900, admin=True), ""))
        self.assertEqual([(c.name, c.value) for c in found],
                         [("biggs_plex · linked to 1 member", "p1"), ("wedge", "p2")])  # never the owner
        found = asyncio.run(self.svc.plex_user_suggestions(FakeInteraction(900, admin=True), "wedge@exa"))
        self.assertEqual([c.value for c in found], ["p2"])  # an email finds them, but isn't shown


class AdminRoleTests(unittest.TestCase):
    """Who counts as an admin in Discord commands (setting admin_role_ids)."""

    def user(self, uid=5, roles=(), manage=False, owner=1):
        return SimpleNamespace(id=uid, guild=SimpleNamespace(owner_id=owner),
                               roles=[SimpleNamespace(id=r) for r in roles],
                               guild_permissions=SimpleNamespace(manage_guild=manage, administrator=False))

    def test_default_owner_and_manage_server(self):
        from woosarr.services.discord_requests import is_admin
        self.assertTrue(is_admin(self.user(uid=1), []))  # the server owner
        self.assertTrue(is_admin(self.user(manage=True), []))
        self.assertFalse(is_admin(self.user(roles=[10]), []))

    def test_chosen_roles_replace_the_discord_permissions(self):
        from woosarr.services.discord_requests import is_admin
        self.assertTrue(is_admin(self.user(roles=[10]), ["10"]))
        self.assertFalse(is_admin(self.user(manage=True), ["10"]))  # Manage Server no longer counts
        self.assertTrue(is_admin(self.user(uid=1), ["10"]))  # the owner always does

    def test_invite_uses_the_admin_roles(self):
        from woosarr.core import app_settings
        h = AppHarness()
        try:
            svc = DiscordService(h.app.state.db, SecretStore(SecretBox(Fernet.generate_key())),
                                 lambda *a, **k: None, plex=None)
            with h.app.state.db.session() as s:
                app_settings.update(s, {"admin_role_ids": ["10"]})
            from unittest import mock

            from woosarr.services import invites
            seen = []

            def create(_s, _gw, **kw):
                seen.append(kw["actor_is_admin"])
                return invites.InviteResult(False, "x")

            with mock.patch.object(invites, "create", create):
                # Manage Server, but not one of the admin roles
                asyncio.run(svc.handle_invite_command(FakeInteraction(501, admin=True), "x@example.com", None))
                member = SimpleNamespace(id=502, guild=None, roles=[SimpleNamespace(id=10)], guild_permissions=None)
                it = FakeInteraction(502)
                it.user = member
                asyncio.run(svc.handle_invite_command(it, "x@example.com", None))
            self.assertEqual(seen, [False, True])
        finally:
            h.close()
