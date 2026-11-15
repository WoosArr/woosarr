"""Fixes from the 2026-11-15 code audit (plan P14-02)."""

import asyncio
import threading
import unittest
from datetime import timedelta
from unittest import mock

from sqlalchemy import select

from tests.helpers import ADMIN, PASSWORD, AppHarness
from tests.test_invite_unlink import Base as InviteBase
from woosarr.db.models import AdminUser, PlexInvite, PlexUser, utcnow
from woosarr.services import inventory


class ApiFixTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.client = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def test_cancelling_an_invite_older_than_the_newest_200(self):
        with self.h.app.state.db.session() as s:
            old = PlexInvite(identifier="old@x.com", discord_user_id="900", invited_by="discord:900",
                             created_at=utcnow() - timedelta(days=30))
            s.add(old)
            s.flush()
            old_id = old.id
            s.add_all([PlexInvite(identifier=f"n{i}@x.com", discord_user_id=str(1000 + i), invited_by="x")
                       for i in range(205)])
        r = self.client.delete(f"/api/v1/invites/{old_id}")
        self.assertEqual(r.status_code, 200, r.text)  # was a 500 (StopIteration)
        self.assertEqual((r.json()["id"], r.json()["status"]), (old_id, "cancelled"))

    def test_wrong_current_password_doesnt_lock_the_account(self):
        for _ in range(6):
            r = self.client.post("/api/v1/auth/password",
                                 json={"current_password": "not-the-password", "new_password": "x" * 12})
            self.assertIn(r.status_code, (400, 429))
        with self.h.app.state.db.session() as s:
            user = s.scalar(select(AdminUser).where(AdminUser.username == ADMIN))
            self.assertIsNone(user.locked_until)
            self.assertEqual(user.failed_logins, 0)
        self.h.app.state.login_limiter.reset()
        self.h.login(ADMIN, PASSWORD)  # still signs in

    def test_picking_a_link_status_role_applies_it_now(self):
        with mock.patch.object(self.h.app.state.discord, "schedule_role_sync") as sync:
            r = self.client.patch("/api/v1/settings", json={"verified_role_id": "123456"})
            self.assertEqual(r.status_code, 200, r.text)
            sync.assert_called_once()
            self.client.patch("/api/v1/settings", json={"bot_status": "hi"})
            sync.assert_called_once()  # other settings don't

    def test_plex_sign_in_returns_to_the_public_url(self):
        self.client.patch("/api/v1/settings", json={"public_url": "https://woosarr.example.com"})
        svc = self.h.app.state.plex
        with mock.patch.object(svc, "start_login", return_value=("p", "https://app.plex.tv/auth")) as start:
            self.assertEqual(self.client.post("/api/v1/plex/login").status_code, 200)
        self.assertEqual(start.call_args.args[0], "https://woosarr.example.com/setup?plex=signed-in")


class InviteCooldownTests(InviteBase):
    def test_admins_arent_held_to_the_link_cooldown(self):
        from woosarr.core import app_settings
        with self.db.session() as s:
            app_settings.update(s, {"link_cooldown_seconds": 300})
        self.assertTrue(self.invite(actor="d9", target="d2", ident="a@x.com", admin=True).ok)
        with self.db.session() as s:
            s.get(PlexInvite, 1).status = "cancelled"
        self.assertTrue(self.invite(actor="d9", target="d2", ident="b@x.com", admin=True).ok)


class IncompleteRosterTests(InviteBase):
    def test_users_missing_from_an_incomplete_list_arent_marked_removed(self):
        self.gw.users = []  # the friends list didn't answer: alice is missing from this answer
        self.gw.roster_complete = False
        with self.db.session() as s, self.assertLogs("woosarr.inventory", "INFO") as logs:
            inventory.refresh_users(s, self.gw)
        with self.db.session() as s:
            self.assertFalse(s.get(PlexUser, "11").removed)
        self.assertIn("list incomplete", "".join(logs.output))
        self.gw.roster_complete = True  # a complete list that still lacks her: removed
        with self.db.session() as s:
            inventory.refresh_users(s, self.gw)
        with self.db.session() as s:
            self.assertTrue(s.get(PlexUser, "11").removed)

    def test_gateway_reports_an_incomplete_roster(self):
        from plexapi.exceptions import NotFound

        from tests.test_plex_api_gateway import make
        fake, gw = make()
        gw.list_users()
        self.assertTrue(gw.roster_complete)
        with self.assertRaises(NotFound):  # complete list: an unknown user really is gone
            gw.get_user_library_ids("4040")
        fake.friends_v2_fails = True
        gw._retries = 0
        gw.list_users()
        self.assertFalse(gw.roster_complete)
        self.assertEqual(gw.get_user_library_ids("4040"), frozenset())  # "no share known", not an error


class BotPermissionChecksTests(unittest.TestCase):
    """Permission checks made on the event loop read the DB in a worker thread (a slow disk mustn't
    block the Discord heartbeat)."""

    def test_features_now_reads_the_db_off_the_loop(self):
        from woosarr.services.discord_bot import DiscordService
        h = AppHarness()
        try:
            svc: DiscordService = h.app.state.discord
            loop_thread = []
            real = h.app.state.db.session

            def session():
                if threading.get_ident() in loop_thread:
                    raise AssertionError("DB opened on the event loop")
                return real()

            async def main():
                loop_thread.append(threading.get_ident())
                with mock.patch.object(svc._db, "session", session):
                    return await svc.features_now()

            report = asyncio.run(main())
            self.assertTrue(any(f["key"] == "commands" for f in report))
        finally:
            h.close()

