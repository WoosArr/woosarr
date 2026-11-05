import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError
from sqlalchemy import select

from tests.helpers import AppHarness
from tests.test_invite_unlink import Base
from tests.test_invite_unlink import GwUser as User
from woosarr.core import app_settings
from woosarr.core.app_settings import AppSettings
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import AuditLog, DiscordLink, PlexInvite, utcnow
from woosarr.services import invite_welcome as welcome
from woosarr.services.discord_bot import DiscordService

VALUES = {"member": "newbie", "plex_username": "newbie\\_plex", "libraries": "Movies and TV", "server": "Bragi"}


class ComposeTests(Base):
    def test_unverified_gets_the_verify_step_and_where_to_find_the_server(self):
        text = welcome.compose(AppSettings(), VALUES, verified=False, has_libraries=True, features=[])
        self.assertIn("linked to the Plex account **newbie\\_plex**. You can see **Movies and TV**.", text)
        self.assertIn("**One more step:** run `/link`", text)
        self.assertNotIn("You'll need this", text)  # nothing needs a verified link
        self.assertTrue(text.endswith("pick **Bragi** from the server list."))

    def test_parts_only_when_they_apply(self):
        st = AppSettings()
        verified = welcome.compose(st, VALUES, verified=True, has_libraries=False, features=["x"])
        self.assertNotIn("One more step", verified)
        self.assertNotIn("You'll need", verified)
        self.assertNotIn("Bragi", verified)
        needs = welcome.needs_verified(st.model_copy(update={"ignore_unverified_links": True}), requests_on=True)
        text = welcome.compose(st, VALUES, verified=False, has_libraries=True, features=needs)
        self.assertIn("You'll need this to request movies and shows with `/request` and have your library "
                      "access follow your Discord roles.", text)
        self.assertEqual(welcome.needs_verified(st, requests_on=False), [])  # Seerr not connected

    def test_texts_are_editable_and_empty_parts_left_out(self):
        st = AppSettings(invite_welcome_text="Hi {member}, you're in as {plex_username} on {server}!",
                         invite_welcome_verify_text="", invite_welcome_find_server_text="  ")
        self.assertEqual(welcome.compose(st, VALUES, verified=False, has_libraries=True, features=[]),
                         "Hi newbie, you're in as newbie\\_plex on Bragi!")

    def test_unknown_placeholders_are_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            AppSettings(invite_welcome_find_server_text="Open {plex_server}")
        self.assertIn("unknown placeholder {plex_server}", str(ctx.exception))
        with self.assertRaises(ValidationError):
            AppSettings(invite_welcome_text="")  # the main text is always sent

    def test_long_messages_fit_discord(self):
        st = AppSettings(invite_welcome_text="x" * 1500, invite_welcome_verify_text="y" * 1500)
        self.assertEqual(len(welcome.compose(st, VALUES, verified=False, has_libraries=False, features=[])), 2000)


class SendTests(Base):
    def setUp(self):
        super().setUp()
        self.discord = DiscordService(self.db, SecretStore(SecretBox(b"0" * 43 + b"=")), self.sync.trigger, None,
                                      self.sync)
        self.discord.send_dm = mock.AsyncMock(return_value=True)  # send_dm itself: test_requests.test_dm_closed
        self.discord._client = SimpleNamespace(is_ready=lambda: True, request_name="request")

    def accept(self):
        self.invite()
        self.gw.users.append(User("22", "newbie", "newbie@x.com"))
        self.gw.shares["22"] = {"1"}
        self.sync.check_invites()
        self.sync.run_now(["22"], "invite accepted")

    def send(self):
        return asyncio.run(self.discord.send_invite_welcomes())

    def test_welcome_sent_once_after_the_invite_is_accepted(self):
        self.accept()
        self.assertEqual(self.send(), 1)
        member, text = self.discord.send_dm.call_args.args
        self.assertEqual(member, "d2")
        self.assertIn("Plex account **newbie**. You can see **Movies**.", text)
        self.assertIn("Sign in with Plex", text)  # invited themselves: unverified
        self.assertIn("You'll need this to request movies and shows with `/request`.", text)  # default setting
        self.assertEqual(self.send(), 0)  # never twice
        with self.db.session() as s:
            entry = s.scalars(select(AuditLog).where(AuditLog.action == "invite_welcome")).one()
            self.assertEqual((entry.target_id, entry.details["sent"]), ("d2", True))

    def test_not_marked_while_the_bot_is_offline(self):
        self.accept()
        self.discord._client = SimpleNamespace(is_ready=lambda: False)
        self.assertEqual(self.send(), 0)
        self.discord._client = SimpleNamespace(is_ready=lambda: True, request_name=None)
        self.assertEqual(self.send(), 1)
        self.assertNotIn("You'll need this", self.discord.send_dm.call_args.args[1])  # no /request: nothing needs it

    def test_closed_dms_are_not_retried(self):
        self.accept()
        self.discord.send_dm.return_value = False
        self.assertEqual(self.send(), 0)
        self.assertEqual(self.send(), 0)
        self.assertEqual(self.discord.send_dm.await_count, 1)

    def test_turned_off_closed_dms_old_and_unlinked_invites(self):
        self.accept()
        with self.db.session() as s:
            app_settings.update(s, {"invite_welcome": False})
        self.assertEqual(self.send(), 0)
        self.discord.send_dm.assert_not_called()
        with self.db.session() as s:
            app_settings.update(s, {"invite_welcome": True})
            inv = s.scalars(select(PlexInvite)).one()
            self.assertIsNotNone(inv.welcomed_at)  # turning it back on doesn't send old ones
            inv.welcomed_at, inv.resolved_at = None, utcnow() - timedelta(days=2)
        self.assertEqual(self.send(), 0)  # accepted too long ago
        with self.db.session() as s:
            inv = s.scalars(select(PlexInvite)).one()
            inv.resolved_at = utcnow()
            s.delete(s.scalars(select(DiscordLink).where(DiscordLink.discord_user_id == "d2")).one())
        self.assertEqual(self.send(), 0)  # unlinked since: nothing to say
        self.discord.send_dm.assert_not_called()



class PreviewApiTests(unittest.TestCase):
    def test_preview_shows_both_messages_and_the_defaults(self):
        h = AppHarness(demo=True)
        try:
            c = h.setup_admin()
            r = c.post("/api/v1/settings/invite-welcome/preview", json={}).json()
            self.assertEqual(r["defaults"]["invite_welcome_text"], welcome.DEFAULT_TEXT)
            self.assertIn("plex_username", r["placeholders"])
            self.assertIn("Sign in with Plex", r["unverified"])
            self.assertNotIn("Sign in with Plex", r["verified"])
            self.assertIn("Demo server", r["verified"])
            r = c.post("/api/v1/settings/invite-welcome/preview",
                       json={"invite_welcome_text": "Hi {member}", "invite_welcome_find_server_text": ""}).json()
            self.assertEqual(r["verified"], "Hi alex")
            bad = c.post("/api/v1/settings/invite-welcome/preview", json={"invite_welcome_text": "{nope}"})
            self.assertEqual(bad.status_code, 422)
            saved = c.patch("/api/v1/settings", json={"invite_welcome_verify_text": "Verify with /link"})
            self.assertEqual(saved.json()["invite_welcome_verify_text"], "Verify with /link")
        finally:
            h.close()
