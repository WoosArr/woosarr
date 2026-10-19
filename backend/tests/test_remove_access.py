"""Admin 'Remove libraries' (live: removing access from friendly_fred did nothing,
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
        self.gw.users.append(GwUser("12", "friendly_fred", "m@x.com"))
        self.gw.shares["12"] = {"1", "2", "3"}
        with self.db.session() as s:
            s.add(PlexUser(plex_user_id="12", username="friendly_fred"))

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


class PreviewAndUndoTests(Base):
    def setUp(self):
        super().setUp()
        self.gw.users.append(GwUser("12", "friendly_fred", "m@x.com"))
        self.gw.shares["12"] = {"1", "2", "3"}
        with self.db.session() as s:
            s.add(PlexUser(plex_user_id="12", username="friendly_fred"))
        self.sync.run_now(None, "cache shares")  # the preview reads the share cache

    def test_preview_matches_the_real_removal(self):
        from woosarr.services import impact

        with self.db.session() as s:
            preview = {p["plex_user_id"]: p for p in impact.removal_preview(s, ["11", "12"], ["1", "3"])}
        results = {r["plex_user_id"]: r for r in self.sync.remove_access(["11", "12"], ["1", "3"], "admin:t")}
        for uid in ("11", "12"):
            self.assertEqual(preview[uid]["removed"], results[uid]["removed"], uid)
            self.assertEqual(preview[uid]["kept_by_roles"], results[uid]["kept_by_roles"], uid)

    def test_undo_restores_shares_and_manual_grants(self):
        with self.db.session() as s:
            s.add(ManualGrant(plex_user_id="12", library_id="2", granted_by="admin:x"))
        [r] = self.sync.remove_access(["12"], ["1", "2"], "admin:t")
        self.assertEqual(self.gw.shares["12"], {"3"})
        [u] = self.sync.restore_access([{"plex_user_id": "12", "library_ids": r["removed"],
                                         "manual_library_ids": r["manual_removed"]}], "admin:t")
        self.assertEqual(u["restored"], ["1", "2"])
        self.assertEqual(self.gw.shares["12"], {"1", "2", "3"})
        with self.db.session() as s:
            self.assertIsNotNone(s.get(ManualGrant, ("12", "2")))
            actions = s.scalars(select(AuditLog.action)).all()
        self.assertIn("access_restored_admin", actions)


class SettingsImpactTests(Base):
    def test_going_live_and_policies(self):
        from woosarr.core.app_settings import AppSettings
        from woosarr.services import impact

        with self.db.session() as s:
            app_settings.update(s, {"dry_run": True})
        self.gw.shares["11"] = {"1"}  # alice is missing TV (2) that her roles grant
        self.sync.run_now(None, "cache")
        with self.db.session() as s:
            live = impact.settings_impact(s, AppSettings.model_validate(
                app_settings.load(s).model_dump() | {"dry_run": False}))
        self.assertEqual(live, [{"plex_user_id": "11", "username": "alice", "gain": ["2"], "lose": []}])
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": False})
            link = s.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == "11"))
            link.verified = False
        self.gw.shares["11"] = {"1", "2"}
        self.sync.run_now(None, "cache")
        with self.db.session() as s:
            ignore = impact.settings_impact(s, AppSettings.model_validate(
                app_settings.load(s).model_dump() | {"ignore_unverified_links": True}))
        self.assertEqual(ignore, [])  # unmanaged users keep what they have: no loss

    def test_endpoints(self):
        h = AppHarness(demo=True)
        try:
            c = h.setup_admin()
            h.app.state.sync.run_now(None, "load")
            c.put("/api/v1/users/1001/link", json={"discord_user_id": "50001"})
            c.put("/api/v1/roles/9002/libraries", json={"library_ids": ["2", "3"]})
            r = c.post("/api/v1/settings/preview", json={"dry_run": False}).json()
            self.assertEqual(r["users_gaining"], 1)
            self.assertEqual(r["users"][0]["gain"], ["3"])  # alice has TV, lacks Anime
            self.assertEqual(c.post("/api/v1/settings/preview", json={"bogus": 1}).status_code, 422)
            p = c.post("/api/v1/users/remove-access/preview",
                       json={"plex_user_ids": ["1001", "1002"], "library_ids": ["2", "6"]}).json()
            by = {x["plex_user_id"]: x for x in p}
            self.assertEqual(by["1001"]["kept_by_roles"], ["2"])
            self.assertEqual(by["1002"]["removed"], ["6"])
            c.patch("/api/v1/settings", json={"dry_run": False})
            done = c.post("/api/v1/users/remove-access", json={"plex_user_ids": ["1002"], "library_ids": ["6"]}).json()
            undo = c.post("/api/v1/users/restore-access", json={"items": [
                {"plex_user_id": "1002", "library_ids": done[0]["removed"]}]}).json()
            self.assertEqual(undo[0]["restored"], ["6"])
        finally:
            h.close()
