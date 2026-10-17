import tempfile
import time
import unittest
from pathlib import Path

from sqlalchemy import select

from woosarr.core import app_settings
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    Library,
    ManualGrant,
    RoleLibrary,
)
from woosarr.db.session import Database, make_engine, migrate
from woosarr.dev import demo
from woosarr.services import inventory
from woosarr.services.sync import SyncService


class SyncServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.gw = demo.make_gateway()
        demo.seed_discord(self.db)
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)
            # alice: Movie Night + Series Fans; bob: Movie Night; carol: Anime Club + Supporters
            for plex, discord, verified in (("1001", "50001", True), ("1002", "50002", False), ("1003", "50003", True)):
                s.add(DiscordLink(plex_user_id=plex, discord_user_id=discord, method="oauth" if verified else "username",
                                  verified=verified, linked_by="self"))
            for role, lib in (("9001", "1"), ("9001", "4"), ("9002", "2"), ("9003", "3"), ("9004", "4")):
                s.add(RoleLibrary(role_id=role, library_id=lib))
        self.svc = SyncService(self.db, lambda: self.gw, debounce_seconds=0.05)

    def tearDown(self):
        self.svc.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def live(self):
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": False})

    def audit(self, action=None):
        with self.db.session() as s:
            stmt = select(AuditLog)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            return list(s.scalars(stmt))

    def test_dry_run_is_default_and_writes_nothing(self):
        report = self.svc.run_now(None, "test")
        self.assertTrue(report.dry_run)
        self.assertEqual(self.gw.writes, [])
        self.assertGreater(report.users_changed, 0)
        planned = self.audit("access_planned")
        self.assertTrue(planned)
        self.assertTrue(all(a.dry_run for a in planned))

    def test_live_run_applies_complete_additive_sets(self):
        self.live()
        report = self.svc.run_now(None, "test")
        self.assertEqual(report.errors, 0, report.error_messages)
        # alice had {1,2}; roles give {1,4,2}; nothing lost (N3), complete set written (N6)
        self.assertEqual(self.gw.shares["1001"], {"1", "2", "4"})
        # bob had {1,6}; 6 is unmanaged (no role maps it) so it stays; gains 4
        self.assertEqual(self.gw.shares["1002"], {"1", "4", "6"})
        # carol had {5} (unmanaged, kept) + Anime + Supporters
        self.assertEqual(self.gw.shares["1003"], {"3", "4", "5"})
        # dave and erin are not linked (N5)
        self.assertEqual(self.gw.shares["1004"], {"6"})
        self.assertEqual(self.gw.shares["1005"], set())
        self.assertEqual({w[0] for w in self.gw.writes}, {"1001", "1002", "1003"})

    def test_second_run_is_a_no_op(self):
        self.live()
        self.svc.run_now(None, "first")
        writes = len(self.gw.writes)
        report = self.svc.run_now(None, "second")
        self.assertEqual(report.users_changed, 0)
        self.assertEqual(len(self.gw.writes), writes)

    def test_role_removal_keeps_access_other_sources_grant(self):
        self.live()
        self.svc.run_now(None, "first")
        with self.db.session() as s:
            member = s.get(DiscordMember, "50003")
            member.role_ids = ["9003"]  # carol loses Supporters
        self.svc.run_now(["1003"], "role removed")
        self.assertEqual(self.gw.shares["1003"], {"3", "5"})
        with self.db.session() as s:
            s.add(ManualGrant(plex_user_id="1003", library_id="4", granted_by="admin:x"))
        self.svc.run_now(["1003"], "manual")
        self.assertEqual(self.gw.shares["1003"], {"3", "4", "5"})

    def test_exempt_library_never_touched(self):
        self.live()
        with self.db.session() as s:
            s.get(Library, "1").is_exempt = True
        self.gw.shares["1003"].add("1")  # carol has Movies via an exempt share
        self.svc.run_now(None, "test")
        self.assertIn("1", self.gw.shares["1003"])  # kept although no role grants it (N4)
        self.assertNotIn("1", self.gw.shares["1005"])

    def test_leave_policy(self):
        self.live()
        self.svc.run_now(None, "first")
        with self.db.session() as s:
            s.get(DiscordMember, "50001").in_guild = False
            s.add(ManualGrant(plex_user_id="1001", library_id="2", granted_by="admin:x"))
        self.svc.run_now(["1001"], "left")
        self.assertEqual(self.gw.shares["1001"], {"2"})  # role access dropped, manual kept
        with self.db.session() as s:
            app_settings.update(s, {"leave_policy": "keep_all"})
            s.get(DiscordMember, "50002").in_guild = False
        before = set(self.gw.shares["1002"])
        self.svc.run_now(["1002"], "left")
        self.assertEqual(self.gw.shares["1002"], before)

    def test_ignore_unverified_links(self):
        self.live()
        with self.db.session() as s:
            app_settings.update(s, {"ignore_unverified_links": True})
        self.svc.run_now(None, "test")
        self.assertNotIn("1002", {w[0] for w in self.gw.writes})  # bob's link is unverified

    def test_unconfigured_plex_skips(self):
        svc = SyncService(self.db, lambda: None)
        report = svc.run_now(None, "test")
        self.assertEqual(report.skipped, "Plex is not configured")

    def test_one_failing_user_does_not_stop_others(self):
        self.live()
        real = self.gw.get_user_library_ids

        def flaky(uid):
            if uid == "1001":
                raise ConnectionError("plex timed out")
            return real(uid)

        self.gw.get_user_library_ids = flaky
        report = self.svc.run_now(None, "test")
        self.assertEqual(report.errors, 1)
        self.assertIn("plex timed out", report.error_messages[0])
        self.assertIn("1003", {w[0] for w in self.gw.writes})

    def test_triggers_are_debounced_on_the_worker(self):
        self.live()
        self.svc.start()
        for _ in range(5):
            self.svc.trigger(["1001"], "burst")
        deadline = time.monotonic() + 5
        while (self.svc.last_report is None or self.svc.running) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.svc.last_report)
        self.assertFalse(self.svc.last_report.full)
        self.assertEqual([w[0] for w in self.gw.writes], ["1001"])  # one write for the burst
        self.assertTrue(self.svc.status()["worker_alive"])

    def test_inventory_marks_removed_libraries_and_renames(self):
        self.gw.libraries = [lib for lib in self.gw.libraries if lib.library_id != "6"]
        self.gw.libraries[0] = type(self.gw.libraries[0])("1", "Films", "movie")
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)
        with self.db.session() as s:
            self.assertTrue(s.get(Library, "6").removed)
            self.assertEqual(s.get(Library, "1").title, "Films")


class SyncWithPlexApiGatewayTests(unittest.TestCase):
    """Full reconcile against the plex.tv-backed gateway uses one batch read."""

    def setUp(self):
        from tests.fake_plextv import MACHINE, FakePlexTV
        from woosarr.plex_api_gateway import PlexApiGateway

        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.tv = FakePlexTV()
        self.tv.shares["11"] = {"id": 9, "sections": {101}}
        self.tv.shares["12"] = {"id": 10, "sections": {102}}
        self.gw = PlexApiGateway(self.tv.account, MACHINE, sleep=lambda _s: None)
        self.svc = SyncService(self.db, lambda: self.gw)
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)
            s.add(DiscordMember(discord_user_id="d1", username="alice", role_ids=["r1"]))
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d1", method="oauth", verified=True, linked_by="self"))
            s.add(RoleLibrary(role_id="r1", library_id="3"))
            app_settings.update(s, {"dry_run": False})

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_full_run_reads_friends_list_once_plus_writes(self):
        self.tv.users_calls = 0
        report = self.svc.run_now(None, "test")
        self.assertEqual(report.errors, 0, report.error_messages)
        self.assertEqual(report.writes, 1)
        # inventory (1) + batch read (1) + drift re-read and write lookup for the one changed user (2)
        self.assertLessEqual(self.tv.users_calls, 4)
        self.assertEqual(self.tv.shares["11"]["sections"], {101, 103})

    def test_user_whose_batch_read_failed_is_retried_alone(self):
        self.gw._retries = 0
        self.tv.broken_users = {"12"}
        report = self.svc.run_now(None, "test")
        self.assertEqual(report.errors, 1)  # 12 still fails on its own retry, and is reported
        self.assertEqual(report.writes, 1)  # 11 was synced regardless
