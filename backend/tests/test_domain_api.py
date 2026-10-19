import unittest

from tests.helpers import AppHarness


class DomainApiTests(unittest.TestCase):
    """Integration tests over HTTP with demo (fake) Plex and Discord data (P11-02)."""

    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()
        self.h.app.state.sync.run_now(None, "test setup")  # inventory into the DB

    def tearDown(self):
        self.h.close()

    def test_status(self):
        body = self.c.get("/api/v1/status").json()
        self.assertTrue(body["dry_run"])
        self.assertTrue(body["plex"]["connected"])
        self.assertEqual(body["counts"]["plex_users"], 5)
        self.assertEqual(body["counts"]["libraries"], 6)

    def test_link_unlink_and_sources(self):
        r = self.c.put("/api/v1/users/1001/link", json={"discord_user_id": "50001"})
        self.assertEqual(r.status_code, 200, r.text)
        user = r.json()
        self.assertTrue(user["link"]["verified"])
        self.assertEqual(user["link"]["method"], "admin")
        self.assertEqual({r["name"] for r in user["roles"]}, {"Movie Night", "Series Fans"})
        # Same Discord account can't be linked to a second Plex user.
        self.assertEqual(self.c.put("/api/v1/users/1002/link", json={"discord_user_id": "50001"}).status_code, 409)
        self.assertEqual(self.c.put("/api/v1/users/1002/link", json={"discord_user_id": "abc"}).status_code, 422)

        self.c.put("/api/v1/roles/9001/libraries", json={"library_ids": ["1", "4"]})
        libs = {lib["library_id"]: lib for lib in self.c.get("/api/v1/users/1001").json()["libraries"]}
        self.assertEqual(libs["1"]["sources"], ["role"])
        self.assertTrue(libs["1"]["shared"])
        self.assertEqual(libs["2"]["sources"], ["external"])  # shared, but no role maps TV
        self.assertFalse(libs["4"]["shared"])  # role grants it; dry run hasn't shared it

        self.assertEqual(self.c.delete("/api/v1/users/1001/link").json()["link"], None)
        self.assertEqual(self.c.delete("/api/v1/users/1001/link").status_code, 404)
        unlinked = self.c.get("/api/v1/users", params={"link": "unlinked"}).json()
        self.assertEqual(len(unlinked), 5)

    def test_lists_sort_case_insensitively(self):
        from woosarr.db.models import Library, PlexUser
        with self.h.app.state.db.session() as s:
            s.add_all([PlexUser(plex_user_id="2001", username="Zed"), PlexUser(plex_user_id="2002", username="adam"),
                       PlexUser(plex_user_id="2003", username="Bea"),
                       Library(library_id="91", title="anime", type="show"), Library(library_id="92", title="Zoo", type="movie")])
        names = [u["username"] for u in self.c.get("/api/v1/users").json()]
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertLess(names.index("adam"), names.index("Bea"))
        titles = [lib["title"] for lib in self.c.get("/api/v1/libraries").json()]
        self.assertEqual(titles, sorted(titles, key=str.lower))
        self.assertLess(titles.index("anime"), titles.index("Zoo"))

    def test_role_mapping_preview_and_audit(self):
        self.c.put("/api/v1/users/1001/link", json={"discord_user_id": "50001"})
        self.c.put("/api/v1/users/1002/link", json={"discord_user_id": "50002"})
        preview = self.c.post("/api/v1/roles/9001/preview", json={"library_ids": ["4"]}).json()
        self.assertEqual(preview["users_gaining"], 2)
        self.assertEqual(self.c.post("/api/v1/roles/9001/preview", json={"library_ids": ["999"]}).status_code, 404)
        r = self.c.put("/api/v1/roles/9001/libraries", json={"library_ids": ["4"]})
        self.assertEqual(r.status_code, 200)
        roles = {role["role_id"]: role for role in self.c.get("/api/v1/roles").json()}
        self.assertEqual(roles["9001"]["library_ids"], ["4"])
        self.assertEqual(roles["9001"]["linked_holders"], 2)
        audit = self.c.get("/api/v1/audit", params={"action": "role_mapping_changed"}).json()["entries"]
        self.assertEqual(audit[0]["details"]["after"], ["4"])
        self.assertEqual(audit[0]["actor"], "admin:admin")
        plans = self.c.get("/api/v1/sync/preview").json()
        self.assertEqual({p["plex_user_id"] for p in plans}, {"1001", "1002"})

    def test_manual_grants_bulk(self):
        r = self.c.post("/api/v1/users/manual-grants",
                        json={"plex_user_ids": ["1004", "1005"], "library_ids": ["5"], "action": "add"})
        self.assertEqual(r.json()["changed"], 2)
        again = self.c.post("/api/v1/users/manual-grants",
                            json={"plex_user_ids": ["1004", "1005"], "library_ids": ["5"], "action": "add"})
        self.assertEqual(again.json()["changed"], 0)
        self.assertEqual(self.c.get("/api/v1/users/1005").json()["manual_library_ids"], ["5"])
        bad = self.c.post("/api/v1/users/manual-grants",
                          json={"plex_user_ids": ["nope"], "library_ids": ["5"], "action": "add"})
        self.assertEqual(bad.status_code, 404)
        r = self.c.post("/api/v1/users/manual-grants",
                        json={"plex_user_ids": ["1005"], "library_ids": ["5"], "action": "remove"})
        self.assertEqual(r.json()["changed"], 1)

    def test_library_exemption(self):
        r = self.c.patch("/api/v1/libraries/6", json={"is_exempt": True})
        self.assertTrue(r.json()["is_exempt"])
        self.assertEqual(self.c.get("/api/v1/status").json()["counts"]["exempt_libraries"], 1)
        self.assertEqual(self.c.patch("/api/v1/libraries/404", json={"is_exempt": True}).status_code, 404)

    def test_settings_validation_and_audit(self):
        self.assertEqual(self.c.patch("/api/v1/settings", json={"reconcile_interval_minutes": 1}).status_code, 422)
        self.assertEqual(self.c.patch("/api/v1/settings", json={"bogus": 1}).status_code, 422)
        r = self.c.patch("/api/v1/settings", json={"dry_run": False, "log_level": "DEBUG"})
        self.assertFalse(r.json()["dry_run"])
        entry = self.c.get("/api/v1/audit", params={"action": "settings_changed"}).json()["entries"][0]
        self.assertEqual(entry["details"]["changes"]["dry_run"], [True, False])

    def test_sync_now_and_members(self):
        self.assertEqual(self.c.post("/api/v1/sync", json={}).status_code, 202)
        self.assertEqual(self.c.post("/api/v1/sync", json={"plex_user_ids": ["1001"]}).json()["count"], 1)
        members = self.c.get("/api/v1/discord/members", params={"q": "ali"}).json()
        self.assertEqual([m["discord_user_id"] for m in members], ["50001"])

    def test_audit_paging(self):
        for i in range(5):
            self.c.patch("/api/v1/libraries/6", json={"is_exempt": i % 2 == 0})
        page = self.c.get("/api/v1/audit", params={"limit": 2}).json()
        self.assertEqual(len(page["entries"]), 2)
        older = self.c.get("/api/v1/audit", params={"limit": 2, "before_id": page["next_before_id"]}).json()
        self.assertLess(older["entries"][0]["id"], page["entries"][-1]["id"])
