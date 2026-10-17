"""Admin 'Remove libraries' (live: removing access from themunchkin did nothing,
because the old button only removed manual grants and they had none)."""

import unittest

from sqlalchemy import select

from tests.helpers import AppHarness
from tests.test_invite_unlink import Base
from woosarr.core import app_settings
from woosarr.db.models import AuditLog, DiscordLink, ManualGrant, PlexUser
from woosarr.plex_gateway import PlexUser as GwUser


class RemoveAccessTests(Base):
    def setUp(self):
        super().setUp()
        # 12 = an unlinked friend with libraries shared directly in Plex ("external").
        self.gw.users.append(GwUser("12", "themunchkin", "m@x.com"))
        self.gw.shares["12"] = {"1", "2", "3"}
        with self.db.session() as s:
            s.add(PlexUser(plex_user_id="12", username="themunchkin"))

    def test_unlinked_user_external_shares_are_removed(self):
        [r] = self.sync.remove_access(["12"], ["1", "2"], "admin:test")
        self.assertEqual((r["removed"], r["kept_by_roles"], r["error"]), (["1", "2"], [], None))
        self.assertEqual(self.gw.shares["12"], {"3"})
        self.assertEqual(self.gw.writes[-1], ("12", frozenset({"3"})))  # complete set written

    def test_linked_user_keeps_role_granted_libraries(self):
        with self.db.session() as s:
            s.add(ManualGrant(plex_user_id="11", library_id="3", granted_by="admin:x"))
        [r] = self.sync.remove_access(["11"], ["1", "3"], "admin:test")
        # alice's roles grant 1 (movies) and 2 (tv); 3 is exempt + manual -> removable
        self.assertEqual((r["removed"], r["kept_by_roles"], r["manual_removed"]), (["3"], ["1"], ["3"]))
        self.assertEqual(self.gw.shares["11"], {"1", "2"})
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(ManualGrant)).all(), [])

    def test_last_library_removal_and_not_shared(self):
        [r] = self.sync.remove_access(["12"], ["1", "2", "3", "9"], "admin:test")
        self.assertEqual(r["not_shared"], ["9"])
        self.assertEqual(self.gw.shares["12"], set())

    def test_dry_run_plans_only(self):
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": True})
        [r] = self.sync.remove_access(["12"], ["1"], "admin:test")
        self.assertEqual(r["removed"], ["1"])
        self.assertEqual(self.gw.shares["12"], {"1", "2", "3"})
        with self.db.session() as s:
            entry = s.scalar(select(AuditLog).where(AuditLog.action == "access_removed_admin"))
            self.assertTrue(entry.dry_run)

    def test_owner_refused(self):
        with self.db.session() as s:
            s.get(PlexUser, "12").is_owner = True
        [r] = self.sync.remove_access(["12"], ["1"], "admin:test")
        self.assertIn("owner", r["error"])
        self.assertEqual(self.gw.shares["12"], {"1", "2", "3"})

    def test_next_sync_does_not_bring_it_back_for_unlinked(self):
        self.sync.remove_access(["12"], ["1"], "admin:test")
        self.sync.run_now(None, "after")
        self.assertNotIn("1", self.gw.shares["12"])
        with self.db.session() as s:
            self.assertIsNone(s.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == "12")))


class RemoveAccessApiTests(unittest.TestCase):
    def test_endpoint(self):
        h = AppHarness(demo=True)
        try:
            c = h.setup_admin()
            h.app.state.sync.run_now(None, "load")
            c.patch("/api/v1/settings", json={"dry_run": False})
            r = c.post("/api/v1/users/remove-access", json={"plex_user_ids": ["1002"], "library_ids": ["6"]})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()[0]
            self.assertEqual((body["username"], body["removed"], body["dry_run"]), ("bob", ["6"], False))
            libs = [lib["library_id"] for lib in c.get("/api/v1/users/1002").json()["libraries"]]
            self.assertNotIn("6", libs)
            self.assertEqual(c.post("/api/v1/users/remove-access",
                                    json={"plex_user_ids": ["nope"], "library_ids": ["6"]}).status_code, 404)
        finally:
            h.close()
