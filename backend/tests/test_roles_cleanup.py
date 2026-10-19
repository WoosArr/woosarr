"""Removing roles that no longer exist in Discord (Roles page)."""

import unittest

from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core import app_settings
from woosarr.db.models import (
    AuditLog,
    DiscordMember,
    DiscordRole,
    Library,
    ReactionPost,
    RoleLibrary,
)

GONE, ALIVE, ORPHAN = "111111111111111111", "222222222222222222", "999999999999999999"


class RoleCleanupTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.triggers = []
        self.h.app.state.sync.trigger = lambda ids, reason, **kw: self.triggers.append(reason)
        with self.db() as s:
            s.add_all([Library(library_id="1", title="Movies", type="movie"), Library(library_id="2", title="TV", type="show"),
                       DiscordRole(role_id=GONE, name="Old Movie Night", deleted=True),
                       DiscordRole(role_id=ALIVE, name="Series")])
            s.flush()
            s.add_all([RoleLibrary(role_id=GONE, library_id="1"), RoleLibrary(role_id=ALIVE, library_id="2"),
                       RoleLibrary(role_id=ORPHAN, library_id="1"),  # role deleted while the bot was offline
                       DiscordMember(discord_user_id="501", username="a", role_ids=[GONE, ALIVE]),
                       ReactionPost(title="Pick", content="x", options=[{"emoji": "🍿", "role_id": GONE},
                                                                        {"emoji": "📺", "role_id": ALIVE}])])
            app_settings.update(s, {"invite_role_ids": [GONE, ALIVE], "username_link_role_ids": [GONE], "verified_role_id": GONE,
                                    "request_role_ids": [GONE]})

    def tearDown(self):
        self.h.close()

    def db(self):
        return self.h.app.state.db.session()

    def test_lists_deleted_roles_with_their_references(self):
        r = self.c.get("/api/v1/roles/deleted")
        self.assertEqual(r.status_code, 200, r.text)
        found = {x["role_id"]: x for x in r.json()}
        self.assertEqual(set(found), {GONE, ORPHAN})
        gone = found[GONE]
        self.assertEqual((gone["name"], gone["library_ids"], gone["members_holding"]), ("Old Movie Night", ["1"], 1))
        self.assertEqual(sorted(gone["settings"]),
                         ["invite_role_ids", "request_role_ids", "username_link_role_ids", "verified_role_id"])
        self.assertEqual(gone["reaction_posts"][0]["emoji"], "🍿")
        self.assertEqual(found[ORPHAN]["name"], f"(unknown role {ORPHAN})")

    def test_remove_one_cleans_every_reference(self):
        r = self.c.delete(f"/api/v1/roles/{GONE}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["sync_queued"])
        with self.db() as s:
            self.assertIsNone(s.get(DiscordRole, GONE))
            self.assertEqual(list(s.scalars(select(RoleLibrary.role_id).where(RoleLibrary.role_id == GONE))), [])
            st = app_settings.load(s)
            self.assertEqual((st.invite_role_ids, st.request_role_ids, st.verified_role_id), ([ALIVE], [], None))
            self.assertEqual(st.username_link_role_ids, [])
            post = s.scalars(select(ReactionPost)).one()
            self.assertEqual([o["role_id"] for o in post.options], [ALIVE])
            self.assertEqual(s.get(DiscordMember, "501").role_ids, [ALIVE])
            self.assertTrue(s.scalar(select(AuditLog).where(AuditLog.action == "role_removed",
                                                            AuditLog.target_id == GONE)))
            self.assertIsNotNone(s.get(DiscordRole, ALIVE))  # untouched
        self.assertEqual(len(self.triggers), 1)

    def test_live_roles_are_refused_and_unknown_is_404(self):
        r = self.c.delete(f"/api/v1/roles/{ALIVE}")
        self.assertEqual((r.status_code, r.json()["detail"]),
                         (409, "this role still exists in Discord; delete it there first"))
        self.assertEqual(self.c.delete("/api/v1/roles/333333333333333333").status_code, 404)

    def test_remove_all_deleted(self):
        r = self.c.post("/api/v1/roles/remove-deleted")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(x["role_id"] for x in r.json()["removed"]), [GONE, ORPHAN])
        self.assertEqual(self.c.get("/api/v1/roles/deleted").json(), [])
        roles = {x["role_id"] for x in self.c.get("/api/v1/roles").json()}
        self.assertEqual(roles, {ALIVE})
        self.assertEqual(self.c.post("/api/v1/roles/remove-deleted").json()["removed"], [])  # idempotent


if __name__ == "__main__":
    unittest.main()
