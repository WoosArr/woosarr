"""Audit entries show names next to ids: members, roles, Plex users, libraries, admins, posts."""

import unittest

from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core import names
from woosarr.db.models import AuditLog, DiscordMember, DiscordRole, Library, PlexUser, ReactionPost
from woosarr.services import audit, audit_names

MEMBER, ROLE, ROLE2 = "284362693538742273", "1561039587932119062", "1184392045718302722"


class AuditNameTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.db = self.h.app.state.db
        names.update({}, {})
        with self.db.session() as s:
            s.add_all([DiscordMember(discord_user_id=MEMBER, username="rwoot", display_name="rWoot"),
                       DiscordRole(role_id=ROLE, name="Movie Night"), DiscordRole(role_id=ROLE2, name="Anime"),
                       PlexUser(plex_user_id="183715749", username="rwoot_plex"),
                       Library(library_id="2", title="Movies", type="movie"),
                       Library(library_id="5", title="TV Shows", type="show"),
                       ReactionPost(id=2, title="Pick your roles", content="x")])

    def tearDown(self):
        self.h.close()

    def entries(self, **params):
        return self.c.get("/api/v1/audit", params=params).json()["entries"]

    def test_a_sync_change_names_the_user_and_libraries(self):
        with self.db.session() as s:
            audit.record(s, "system", "access_changed", "plex_user", "183715749", add=["2"], remove=["5"],
                         reasons={"2": "granted by role", "5": "no role or manual grant keeps it"}, trigger="startup")
        e = self.entries(action="access_changed")[0]
        self.assertEqual((e["actor_label"], e["target_label"]), ("WooSarr", "Plex rwoot_plex"))
        self.assertEqual(e["details_named"]["add"], ["Movies (2)"])
        self.assertEqual(e["details_named"]["remove"], ["TV Shows (5)"])
        self.assertEqual(e["details_named"]["reasons"], {"Movies (2)": "granted by role",
                                                         "TV Shows (5)": "no role or manual grant keeps it"})
        self.assertEqual(e["details"]["add"], ["2"])  # the raw ids stay for API users

    def test_members_roles_and_same_numbers_of_different_kinds(self):
        with self.db.session() as s:
            audit.record(s, f"discord:{MEMBER}", "reaction_added", "reaction_post", "2", emoji="🎬", role_ids=[ROLE])
            # library 2 and reaction post 2 in one entry: each named as what it is.
            audit.record(s, "admin:dave", "role_removed", "role", ROLE, role_name="Movie Night", library_ids=["2"],
                         reaction_posts=[2])
            audit.record(s, "admin:dave", "discord_roles_changed_admin", "discord_role", f"{ROLE},{ROLE2}",
                         add=[ROLE], remove=[ROLE2], changed=[{"discord_user_id": MEMBER, "added": [ROLE]}])
        removed, changed, reacted = (self.entries(action=a)[0] for a in
                                     ("role_removed", "discord_roles_changed_admin", "reaction_added"))
        self.assertEqual((reacted["actor_label"], reacted["target_label"]), ("@rWoot", "Pick your roles"))
        self.assertEqual(reacted["details_named"]["role_ids"], [f"role Movie Night ({ROLE})"])
        self.assertEqual(removed["target_label"], "role Movie Night")
        self.assertEqual(removed["details_named"]["library_ids"], ["Movies (2)"])
        self.assertEqual(removed["details_named"]["reaction_posts"], ["Pick your roles (2)"])
        self.assertEqual(changed["target_label"], "role Movie Night, role Anime")
        self.assertEqual(changed["details_named"]["remove"], [f"role Anime ({ROLE2})"])
        self.assertEqual(changed["details_named"]["changed"][0]["discord_user_id"], f"@rWoot ({MEMBER})")

    def test_names_are_kept_as_they_were_when_written(self):
        with self.db.session() as s:
            audit.record(s, "admin:dave", "role_mapping_changed", "role", ROLE, before=["5"], after=["2", "5"])
        with self.db.session() as s:
            s.get(DiscordRole, ROLE).name = "Film Club"
            s.delete(s.get(Library, "5"))
        e = self.entries(action="role_mapping_changed")[0]
        self.assertEqual(e["target_label"], "role Movie Night")
        self.assertEqual(e["details_named"]["after"], ["Movies (2)", "TV Shows (5)"])

    def test_who_and_what_for_other_kinds(self):
        with self.db.session() as s:
            audit.record(s, "ip:10.0.0.9", "login_failed", "admin", "2")  # a typed username, not admin #2
            audit.record(s, "admin-id:1", "sso_test", "oidc_provider", "pocketid", allowed=True)
            audit.record(s, f"discord:{MEMBER}", "link_failed", "discord_user", "999999999999999999", reason="x")
        failed, sso, link = (self.entries(action=a)[0] for a in ("login_failed", "sso_test", "link_failed"))
        self.assertEqual((failed["actor_label"], failed["target_label"]), ("ip:10.0.0.9", "admin 2"))
        self.assertEqual(sso["actor_label"], "admin")  # setup_admin()'s username
        self.assertEqual(link["target_label"], "discord user 999999999999999999")  # unknown member: the id

    def test_older_entries_get_names_once(self):
        with self.db.session() as s:  # as written before 1.2: no names
            s.add(AuditLog(actor=f"discord:{MEMBER}", action="link_self", target_type="plex_user",
                           target_id="183715749", details={"outcome": "linked"}))
            s.add(AuditLog(actor="system", action="access_changed", target_type="plex_user", target_id="183715749",
                           details={"add": ["2"]}))
        # Shown with today's names straight away, before the update reaches them.
        e = self.entries(action="link_self")[0]
        self.assertEqual((e["actor_label"], e["target_label"]), ("@rWoot", "Plex rwoot_plex"))
        with self.assertLogs("woosarr.audit", "INFO") as logs:
            self.assertEqual(audit_names.backfill(self.db, batch=1), 2)
        self.assertIn("added names to 2 older entries", logs.output[-1])
        self.assertEqual(audit_names.backfill(self.db), 0)  # one time: nothing left to do
        with self.db.session() as s:
            row = s.scalars(select(AuditLog).where(AuditLog.action == "access_changed")).one()
            self.assertEqual(row.names, {"plex:183715749": "Plex rwoot_plex", "library:2": "Movies"})

    def test_recent_activity_says_who(self):
        with self.db.session() as s:
            audit.record(s, f"discord:{MEMBER}", "link_self", "plex_user", "183715749", outcome="linked")
        rows = self.c.get("/api/v1/dashboard/insights").json()["activity"]
        link = next(r for r in rows if r["action"] == "link_self")
        self.assertEqual((link["actor_label"], link["target_label"]), ("@rWoot", "Plex rwoot_plex"))
