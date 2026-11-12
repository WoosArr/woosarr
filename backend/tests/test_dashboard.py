"""Dashboard: grouped errors with "mark resolved", widget data, per-admin layout,
and the Users page "linked" filter."""

import logging
import time
import unittest
from datetime import timedelta

from tests.helpers import PASSWORD, AppHarness
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    ErrorResolution,
    Library,
    LibraryState,
    MediaRequest,
    PlexUser,
    RoleLibrary,
    utcnow,
)
from woosarr.services import error_digest


class ErrorDigestTests(unittest.TestCase):
    def test_messages_with_different_ids_group_together(self):
        a = error_digest.fingerprint("ERROR", "woosarr.app", "unhandled error request_id=c2fce138adf3 POST /x/1")
        b = error_digest.fingerprint("ERROR", "woosarr.app", "unhandled error request_id=11035671743b POST /x/22")
        c = error_digest.fingerprint("ERROR", "woosarr.app", "something else entirely")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(error_digest.fingerprint("CRITICAL", "x", "m"), error_digest.fingerprint("ERROR", "x", "m"))
        self.assertNotEqual(error_digest.fingerprint("WARNING", "x", "m"), error_digest.fingerprint("ERROR", "x", "m"))


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.log = logging.getLogger("woosarr.testdash")

    def tearDown(self):
        self.h.close()

    def status(self):
        s = self.c.get("/api/v1/status").json()
        return s["recent_warnings"], s["recent_errors"]

    def test_open_requests_and_roles_that_grant_libraries(self):
        from woosarr.db.models import DiscordRole, Library, MediaRequest, RoleLibrary
        with self.h.app.state.db.session() as s:
            s.add_all([MediaRequest(seerr_request_id=i, discord_user_id="5", seerr_user_id=1, media_type="movie",
                                    tmdb_id=i, title=f"Film {i}", status=st)
                       for i, st in enumerate(["pending", "approved", "available", "declined"] + ["pending"] * 10)])
            s.add_all([DiscordRole(role_id="801", name="Movie Night"), DiscordRole(role_id="802", name="Nothing"),
                       Library(library_id="1", title="Movies", type="movie"),
                       Library(library_id="2", title="4K", type="movie")])
            s.flush()
            s.add_all([RoleLibrary(role_id="801", library_id="1"), RoleLibrary(role_id="801", library_id="2")])
        ins = self.c.get("/api/v1/dashboard/insights").json()
        self.assertEqual((ins["requests"]["open_count"], len(ins["requests"]["open"])), (12, 10))
        self.assertTrue(all(r["status"] in ("pending", "approved") for r in ins["requests"]["open"]))
        self.assertEqual([(r["name"], r["libraries"]) for r in ins["role_libraries"]], [("Movie Night", ["4K", "Movies"])])
        ids = [w["id"] for w in self.c.get("/api/v1/dashboard/layout").json()["widgets"]]
        self.assertIn("open_requests", ids)
        self.assertIn("role_libraries", ids)

    def test_widgets_can_be_copied_and_renamed(self):
        base = self.c.get("/api/v1/dashboard/layout").json()["widgets"]
        mine = [{"id": "libraries", "size": "m", "title": "  All   libraries "},
                {"id": "libraries-2", "size": "xs", "view": "donut", "title": "Libraries (chart)"},
                {"id": "libraries-3", "size": "m", "hidden": True},  # a hidden copy is removed
                {"id": "roles-2", "size": "s", "title": ""}]
        mine += [w for w in base if w["id"] != "libraries"]
        r = self.c.put("/api/v1/dashboard/layout", json={"widgets": mine})
        self.assertEqual(r.status_code, 200, r.text)
        got = {w["id"]: w for w in r.json()["widgets"]}
        self.assertEqual(got["libraries"]["title"], "All libraries")  # one line, trimmed
        self.assertEqual((got["libraries-2"]["view"], got["libraries-2"]["title"]), ("donut", "Libraries (chart)"))
        self.assertIsNone(got["roles-2"]["title"])  # empty = the usual name
        self.assertNotIn("libraries-3", got)
        for bad in ("libraries-1", "nope-2", "libraries-x", "libraries-2-2"):
            self.assertEqual(self.c.put("/api/v1/dashboard/layout", json={"widgets": [{"id": bad}]}).status_code,
                             422, bad)
        self.assertEqual(self.c.put("/api/v1/dashboard/layout", json={"widgets": [
            {"id": "roles", "title": "x" * 61}]}).status_code, 422)

    def test_copies_can_be_filtered(self):
        base = self.c.get("/api/v1/dashboard/layout").json()["widgets"]
        mine = [{"id": "libraries-2", "filter": {"libraries": ["1", "4"]}},
                {"id": "requests", "filter": {"statuses": ["pending", "approved"]}},
                {"id": "errors", "filter": {"levels": ["account"]}},
                {"id": "roles-2", "filter": {"roles": []}}]  # nothing chosen = no filter
        mine += [w for w in base if w["id"] not in ("requests", "errors")]
        got = {w["id"]: w for w in self.c.put("/api/v1/dashboard/layout", json={"widgets": mine}).json()["widgets"]}
        self.assertEqual(got["libraries-2"]["filter"]["libraries"], ["1", "4"])
        self.assertEqual(got["requests"]["filter"]["statuses"], ["pending", "approved"])
        self.assertEqual(got["errors"]["filter"]["levels"], ["account"])
        self.assertIsNone(got["roles-2"]["filter"])
        for bad in ({"statuses": ["lost"]}, {"levels": ["debug"]}, {"roles": ["abc"]}):
            r = self.c.put("/api/v1/dashboard/layout", json={"widgets": [{"id": "requests", "filter": bad}]})
            self.assertEqual(r.status_code, 422, bad)

    def test_blank_widgets_repeat_and_vanish_when_hidden(self):
        base = self.c.get("/api/v1/dashboard/layout").json()["widgets"]
        spacers = [{"id": "spacer-1", "size": "s"}, {"id": "spacer-2", "size": "xs"},
                   {"id": "spacer-3", "size": "m", "hidden": True}]
        r = self.c.put("/api/v1/dashboard/layout", json={"widgets": spacers + base})
        self.assertEqual(r.status_code, 200, r.text)
        ids = [w["id"] for w in r.json()["widgets"]]
        self.assertEqual(ids[:2], ["spacer-1", "spacer-2"])
        self.assertNotIn("spacer-3", ids)  # hidden = removed
        for bad in ("spacer-x", "spacer", "nope"):
            self.assertEqual(self.c.put("/api/v1/dashboard/layout",
                                        json={"widgets": [{"id": bad}]}).status_code, 422, bad)
        dup = self.c.put("/api/v1/dashboard/layout", json={"widgets": [{"id": "spacer-1"}, {"id": "spacer-1"}]})
        self.assertEqual(dup.status_code, 422)

    def test_new_install_starts_with_the_setup_checklist(self):
        layout = self.c.get("/api/v1/dashboard/layout").json()
        self.assertFalse(layout["custom"])
        first = layout["widgets"][0]
        self.assertEqual((first["id"], first["size"], first["hidden"], first["title"]), ("setup", "l", False, None))

    def test_discord_member_link_counts(self):
        from woosarr.db.models import DiscordLink, DiscordMember, PlexUser
        with self.h.app.state.db.session() as s:
            s.add_all([PlexUser(plex_user_id="p1", username="a"), PlexUser(plex_user_id="p2", username="b"),
                       DiscordMember(discord_user_id="1", username="v"), DiscordMember(discord_user_id="2", username="u"),
                       DiscordMember(discord_user_id="3", username="none"),
                       DiscordMember(discord_user_id="4", username="bot", is_bot=True),
                       DiscordMember(discord_user_id="5", username="left", in_guild=False)])
            s.flush()
            s.add_all([DiscordLink(plex_user_id="p1", discord_user_id="1", method="oauth", verified=True, linked_by="self"),
                       DiscordLink(plex_user_id="p2", discord_user_id="2", method="username", verified=False,
                                   linked_by="self"),
                       DiscordLink(plex_user_id="p2", discord_user_id="5", method="oauth", verified=True,
                                   linked_by="self")])
        links = self.c.get("/api/v1/dashboard/insights").json()["links"]
        self.assertEqual((links["members"], links["members_verified"], links["members_unverified"],
                          links["members_unlinked"]), (3, 1, 1, 1))  # bots and leavers don't count
        ids = [w["id"] for w in self.c.get("/api/v1/dashboard/layout").json()["widgets"]]
        self.assertIn("stat_discord_users", ids)
        self.assertIn("stat_discord_linked", ids)

    def test_time_drift_from_discords_clock(self):
        from types import SimpleNamespace
        self.assertIsNone(self.c.get("/api/v1/dashboard/insights").json()["clock"])  # no bot, no measurement
        self.h.app.state.discord = SimpleNamespace(timing=SimpleNamespace(measured=False, offset=0.0))
        self.assertIsNone(self.c.get("/api/v1/dashboard/insights").json()["clock"])  # Discord not set up
        self.h.app.state.discord_status = lambda: {"configured": True, "connected": False, "connecting": True}
        self.assertEqual(self.c.get("/api/v1/dashboard/insights").json()["clock"], {"offset_seconds": None})
        self.h.app.state.discord = SimpleNamespace(timing=SimpleNamespace(measured=True, offset=38.24))
        self.assertEqual(self.c.get("/api/v1/dashboard/insights").json()["clock"], {"offset_seconds": 38.2})
        self.assertIn("stat_clock", [w["id"] for w in self.c.get("/api/v1/dashboard/layout").json()["widgets"]])

    def test_emoji_slots_only_while_the_bot_is_connected(self):
        self.assertIsNone(self.c.get("/api/v1/dashboard/insights").json()["emoji"])  # bot not connected
        self.h.app.state.emoji_guild = type("G", (), {"emoji_slots": lambda self: {"limit": 50, "static": 12,
                                                                                    "animated": 3}})()
        self.assertEqual(self.c.get("/api/v1/dashboard/insights").json()["emoji"],
                         {"limit": 50, "static": 12, "animated": 3})
        ids = [w["id"] for w in self.c.get("/api/v1/dashboard/layout").json()["widgets"]]
        self.assertIn("stat_emoji", ids)
        self.assertIn("stat_emoji_animated", ids)

    def test_connections_say_connecting_while_the_bot_starts(self):
        self.h.app.state.discord_status = lambda: {"configured": True, "connected": False, "connecting": True,
                                                   "detail": "Connecting…"}
        d = self.c.get("/api/v1/status").json()["discord"]
        self.assertEqual((d["connected"], d["connecting"]), (False, True))

    def test_banner_counts_problems_and_occurrences(self):
        for _ in range(5):
            self.log.error("Ignoring exception in autocomplete (User: %s)", 100000000000000000 + _)
        s = self.c.get("/api/v1/status").json()
        self.assertEqual((s["recent_errors"], s["open_errors"]), (5, 1))  # one problem, five times

    def errors(self, level="WARNING"):
        return {g["message"].split(" ", 1)[0]: g for g in self.c.get(f"/api/v1/errors?level={level}").json()}

    def test_resolve_hides_what_happened_and_repeats_show_again(self):
        base_w, base_e = self.status()
        for i in range(3):
            self.log.error("boom-a while publishing post %s", i)
        self.log.error("boom-b other problem")
        self.log.warning("warn-c flaky thing")
        self.assertEqual(self.status(), (base_w + 1, base_e + 4))
        groups = self.errors()
        self.assertEqual((groups["boom-a"]["count"], groups["boom-a"]["level"]), (3, "ERROR"))
        self.assertEqual(set(self.errors("ERROR")), set(self.errors("ERROR")) - {"warn-c"})

        fp = groups["boom-a"]["fingerprint"]
        r = self.c.put(f"/api/v1/errors/{fp}/resolved")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["count"], body["hidden"], body["resolved_by"]), (0, 3, "admin:admin"))
        self.assertIsNotNone(body["resolved_until"])  # when it was resolved
        self.assertEqual(self.status(), (base_w + 1, base_e + 1))  # only boom-b left
        time.sleep(0.01)

        # Happens again right away: it's back (only the 3 earlier ones stay hidden).
        self.log.error("boom-a while publishing post 99")
        self.assertEqual((self.errors()["boom-a"]["count"], self.errors()["boom-a"]["hidden"]), (1, 3))
        self.assertEqual(self.status(), (base_w + 1, base_e + 2))
        self.assertEqual(self.errors()["boom-a"]["resolved_by"], "admin:admin")  # still says who resolved the older ones

        # A resolution saved by an older version (until = resolve time + 24 h) hides no repeats either.
        with self.h.app.state.db.session() as s:
            s.get(ErrorResolution, fp).until = utcnow() + timedelta(hours=24)
        self.log.error("boom-a while publishing post 100")
        self.assertEqual((self.errors()["boom-a"]["count"], self.errors()["boom-a"]["hidden"]), (2, 3))

        self.assertEqual(self.c.delete(f"/api/v1/errors/{fp}/resolved").status_code, 204)
        self.assertEqual(self.errors()["boom-a"]["count"], 5)  # unresolved: all of them
        self.assertEqual(self.c.delete(f"/api/v1/errors/{fp}/resolved").status_code, 404)
        self.assertEqual(self.c.put("/api/v1/errors/0123456789abcdef/resolved").status_code, 404)
        self.assertEqual(self.c.put("/api/v1/errors/NOT-HEX/resolved").status_code, 422)
        with self.h.app.state.db.session() as s:
            actions = [a.action for a in s.query(AuditLog).filter(AuditLog.action.like("error_%"))]
        self.assertEqual(actions, ["error_resolved", "error_unresolved"])

    def test_warnings_survive_a_busy_info_log(self):
        self.log.error("kept-error survives")
        for i in range(self.h.settings.log_buffer_size + 50):
            self.log.info("chatter %s", i)
        self.assertIn("kept-error", self.errors("ERROR"))

    def test_read_key_cannot_resolve(self):
        key = self.c.post("/api/v1/api-keys", json={"name": "ro", "scope": "read"}).json()["key"]
        self.log.error("boom-r read only")
        fp = self.errors()["boom-r"]["fingerprint"]
        ro = self.h.new_client()
        ro.headers["Authorization"] = f"Bearer {key}"
        self.assertEqual(ro.get("/api/v1/errors").status_code, 200)
        self.assertEqual(ro.put(f"/api/v1/errors/{fp}/resolved").status_code, 403)

    def test_insights(self):
        with self.h.app.state.db.session() as s:
            s.add_all([PlexUser(plex_user_id="1", username="owner", is_owner=True),
                       PlexUser(plex_user_id="11", username="alice"), PlexUser(plex_user_id="12", username="bob"),
                       PlexUser(plex_user_id="13", username="carol"),
                       Library(library_id="1", title="Movies", type="movie"),
                       Library(library_id="2", title="Kids", type="movie", is_exempt=True)])
            s.flush()
            s.add_all([DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True,
                                   linked_by="self"),
                       DiscordLink(plex_user_id="12", discord_user_id="502", method="username", verified=False,
                                   linked_by="self"),
                       LibraryState(plex_user_id="11", library_id="1"),
                       MediaRequest(seerr_request_id=7, discord_user_id="501", plex_user_id="11", seerr_user_id=3,
                                    media_type="movie", tmdb_id=603, title="The Matrix", year="1999"),
                       MediaRequest(seerr_request_id=8, discord_user_id="503", plex_user_id=None, seerr_user_id=4,
                                    media_type="tv", tmdb_id=1399, title="Some Show"),
                       DiscordRole(role_id="901", name="Movies"), DiscordRole(role_id="902", name="Bots", managed=True),
                       DiscordRole(role_id="903", name="Fans"), RoleLibrary(role_id="901", library_id="2"),
                       DiscordMember(discord_user_id="501", username="a", role_ids=["901", "902"]),
                       DiscordMember(discord_user_id="503", username="c", role_ids=["901", "903"]),
                       AuditLog(actor="system", action="access_changed", target_type="plex_user", target_id="11"),
                       AuditLog(actor="system", action="access_planned", target_type="plex_user", target_id="12",
                                dry_run=True),
                       AuditLog(actor="ip:1.2.3.4", action="login_failed", target_type="admin", target_id="x")])
        r = self.c.get("/api/v1/dashboard/insights")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["links"], {"plex_users": 4, "linked": 2, "verified": 1, "unverified": 1, "unlinked": 2,
                                      "members": 2, "members_unlinked": 1, "members_verified": 1,
                                      "members_unverified": 0})  # the owner counts as a Plex user
        users = {lib["title"]: lib["users"] for lib in d["libraries"]}
        self.assertEqual(users, {"Movies": 2, "Kids": 1})  # alice's share + the owner, who sees every library
        today = d["access_by_day"][-1]
        self.assertEqual((len(d["access_by_day"]), today["changed"], today["planned"], today["users"]), (14, 1, 1, 2))
        self.assertEqual(d["setup"]["unmanaged_libraries"], 1)  # Movies: no role, not exempt
        self.assertEqual(d["security"]["failed_logins_24h"], 1)
        self.assertGreaterEqual(d["security"]["sessions"], 1)
        self.assertNotIn("access_changed", [a["action"] for a in d["activity"]])
        self.assertFalse(d["requests"]["configured"])
        who = {r["title"]: (r["plex_username"], r["discord_name"]) for r in d["requests"]["recent"]}
        self.assertEqual(who, {"The Matrix": ("alice", "a"), "Some Show": (None, "c")})
        self.assertEqual([(r["name"], r["members"], r["linked"], r["libraries"]) for r in d["roles"]],
                         [("Movies", 2, 1, 1), ("Fans", 1, 0, 0)])  # bot-managed role left out

    def test_account_takeover_alerts(self):
        from woosarr.core import app_settings
        from woosarr.linking import PlexIdentity
        from woosarr.services import link_service
        db = self.h.app.state.db
        with db.session() as s:
            s.add_all([PlexUser(plex_user_id="11", username="bob"),
                       DiscordMember(discord_user_id="50001", username="alice", display_name="Alice"),
                       DiscordMember(discord_user_id="50002", username="mallory")])
            app_settings.update(s, {"link_cooldown_seconds": 0})
        with db.session() as s:
            self.assertTrue(link_service.link_by_username(s, "50001", "bob").ok)
        with db.session() as s:
            self.assertFalse(link_service.link_by_username(s, "50002", "bob").ok)  # alice holds it
        with db.session() as s:
            self.assertFalse(link_service.link_by_username(s, "50002", "bob").ok)  # again: same group
        alerts = [g for g in self.c.get("/api/v1/errors").json() if g["logger"] == "woosarr.accounts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["count"], alerts[0]["level"]), (2, "WARNING"))
        self.assertEqual(alerts[0]["message"], "mallory (Discord 50002) tried to link Plex account bob by typing its "
                                               "username, which is linked to Alice (Discord 50001). Refused.")
        with db.session() as s:  # signing in with Plex beats alice's typed claim
            self.assertTrue(link_service.link_by_identity(s, "50002", PlexIdentity("11", "bob")).ok)
        r = self.c.put("/api/v1/users/11/link", json={"discord_user_id": "50001"})  # admin adds alice back: shared
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(x["discord_user_id"] for x in r.json()["links"]), ["50001", "50002"])
        messages = [g["message"] for g in self.c.get("/api/v1/errors").json() if g["logger"] == "woosarr.accounts"]
        self.assertEqual(messages[:2], [
            "Admin admin added Alice (Discord 50001) to Plex account bob, which is also linked to mallory (Discord 50002).",
            ("mallory (Discord 50002) took over Plex account bob by signing in with Plex. "
             "The unverified link of Alice (Discord 50001) was removed.")])
        # Alerts can be resolved like any other.
        fp = next(g["fingerprint"] for g in self.c.get("/api/v1/errors").json() if "tried to link" in g["message"])
        self.assertEqual(self.c.put(f"/api/v1/errors/{fp}/resolved").json()["count"], 0)

    def test_layout_per_admin_with_defaults(self):
        r = self.c.get("/api/v1/dashboard/layout").json()
        self.assertFalse(r["custom"])
        ids = [w["id"] for w in r["widgets"]]
        self.assertEqual(ids[:2], ["setup", "integrations"])
        self.assertEqual(len(ids), len(set(ids)))
        mine = [{"id": "errors", "size": "l"}, {"id": "stat_linked", "size": "xs", "view": "donut"},
                {"id": "integrations", "size": "m", "hidden": True}]
        r = self.c.put("/api/v1/dashboard/layout", json={"widgets": mine})
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()["widgets"]
        self.assertEqual([w["id"] for w in got[:3]], ["errors", "stat_linked", "integrations"])
        self.assertEqual((got[1]["size"], got[1]["view"], got[0]["view"]), ("xs", "donut", "default"))
        self.assertTrue(all(w["hidden"] for w in got[3:]))  # not listed -> hidden at the end
        self.assertEqual(self.c.get("/api/v1/dashboard/layout").json()["widgets"], got)
        self.assertEqual(self.c.put("/api/v1/dashboard/layout",
                                    json={"widgets": [{"id": "nope"}]}).status_code, 422)
        for bad in ({"id": "errors", "size": "xl"}, {"id": "errors", "view": "pie3d"}):
            self.assertEqual(self.c.put("/api/v1/dashboard/layout", json={"widgets": [bad]}).status_code, 422)
        self.assertEqual(self.c.put("/api/v1/dashboard/layout",
                                    json={"widgets": [{"id": "errors"}, {"id": "errors"}]}).status_code, 422)
        # another admin has their own layout
        self.c.post("/api/v1/admins", json={"username": "second", "password": PASSWORD})
        other = self.h.login("second", PASSWORD)
        self.assertFalse(other.get("/api/v1/dashboard/layout").json()["custom"])
        self.assertFalse(self.c.delete("/api/v1/dashboard/layout").json()["custom"])

    def test_users_linked_filter(self):
        with self.h.app.state.db.session() as s:
            s.add_all([PlexUser(plex_user_id="11", username="alice"), PlexUser(plex_user_id="12", username="bob"),
                       PlexUser(plex_user_id="13", username="carol")])
            s.flush()
            s.add_all([DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True,
                                   linked_by="self"),
                       DiscordLink(plex_user_id="12", discord_user_id="502", method="username", verified=False,
                                   linked_by="self")])

        def names(link):
            return sorted(u["username"] for u in self.c.get(f"/api/v1/users?link={link}").json())

        self.assertEqual(names("linked"), ["alice", "bob"])
        self.assertEqual(names("verified"), ["alice"])
        self.assertEqual(names("unverified"), ["bob"])
        self.assertEqual(names("unlinked"), ["carol"])

