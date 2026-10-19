import unittest

import requests

from tests.fake_plextv import MACHINE, FakePlexTV
from tests.test_plex_contract import GatewayContract
from woosarr.plex_api_gateway import PlexApiGateway, PlexAuthError


def make(fake=None):
    fake = fake or FakePlexTV()
    gw = PlexApiGateway(fake.account, MACHINE, sleep=lambda _s: None)
    return fake, gw


class PlexApiGatewayContractTests(GatewayContract, unittest.TestCase):
    """The shared contract against the python-plexapi gateway on a fake plex.tv."""

    def make_gateway(self):
        _fake, gw = make()
        gw.set_user_libraries("11", ["1"])
        return gw, "11", ["1", "2", "3"]


class PlexApiGatewayTests(unittest.TestCase):
    def test_libraries_come_from_plextv_without_a_server_connection(self):
        fake, gw = make()
        libs = gw.list_libraries()
        self.assertEqual([(lib.library_id, lib.title) for lib in libs][:2], [("1", "Movies"), ("2", "TV")])
        self.assertEqual(fake.calls[0][1], f"https://plex.tv/api/servers/{MACHINE}")

    def test_users_include_home_users(self):
        _fake, gw = make()
        users = {u.plex_user_id: u for u in gw.list_users()}
        self.assertTrue(users["13"].is_managed)
        self.assertFalse(users["11"].is_managed)

    def test_only_shared_sections_count(self):
        fake, gw = make()
        fake.shares["11"] = {"id": 9, "sections": {101, 103}}
        self.assertEqual(gw.get_user_library_ids("11"), frozenset({"1", "3"}))
        self.assertEqual(gw.get_user_library_ids("12"), frozenset())

    def test_writes_plextv_ids_by_key_not_title(self):
        fake, gw = make()  # two libraries are titled "Movies"
        gw.set_user_libraries("11", ["4"])
        method, url, body = fake.calls[-1]
        self.assertEqual(method, "post")
        self.assertEqual(body["shared_server"], {"library_section_ids": [104], "invited_id": 11})
        gw.set_user_libraries("11", ["1", "4"])
        method, url, body = fake.calls[-1]
        self.assertEqual(method, "put")
        self.assertTrue(url.endswith(f"/shared_servers/{fake.shares['11']['id']}"))
        self.assertEqual(sorted(body["shared_server"]["library_section_ids"]), [101, 104])

    def test_removing_the_last_library_deletes_the_share(self):
        fake, gw = make()
        gw.set_user_libraries("11", ["2"])
        gw.set_user_libraries("11", [])
        self.assertEqual(fake.calls[-1][0], "delete")
        self.assertNotIn("11", fake.shares)
        calls = len(fake.calls)
        gw.set_user_libraries("12", [])  # no share: nothing to do
        self.assertEqual(len([c for c in fake.calls[calls:] if c[0] != "get"]), 0)

    def test_unknown_library_rejected(self):
        _fake, gw = make()
        with self.assertRaises(ValueError):
            gw.set_user_libraries("11", ["999"])

    def test_reads_retry_on_transient_errors(self):
        fake, gw = make()
        fake.fail_next = [requests.ConnectionError("boom"), requests.Timeout("slow")]
        self.assertEqual(len(gw.list_libraries()), 4)
        self.assertTrue(gw.state.ok)

    def test_retries_that_succeed_raise_no_alert(self):
        fake, gw = make()
        fake.fail_next = [requests.ConnectionError("reset"), requests.ConnectionError("reset again")]
        with self.assertLogs("woosarr.plex", level="INFO") as cm:
            gw.list_libraries()
        retries = [line for line in cm.output if "retry" in line]
        self.assertEqual(len(retries), 2)
        self.assertTrue(all(line.startswith("INFO") for line in cm.output), cm.output)

    def test_exhausted_retries_are_one_alert(self):
        fake, gw = make()
        fake.fail_next = [requests.ConnectionError("down")] * (gw._retries + 1)
        with self.assertLogs("woosarr.plex", level="INFO") as cm, self.assertRaises(requests.ConnectionError):
            gw.list_libraries()
        alerts = [line for line in cm.output if not line.startswith("INFO")]
        self.assertEqual(alerts, ["ERROR:woosarr.plex:plex: ConnectionError: down"])
        self.assertTrue(any("gave up after 3 retries" in line for line in cm.output))
        self.assertFalse(gw.state.ok)

    def test_writes_do_not_retry_after_the_request_may_have_landed(self):
        fake, gw = make()
        gw.list_libraries()
        gw._users()
        fake.fail_next = [requests.ReadTimeout("no answer")]
        with self.assertRaises(requests.ReadTimeout):
            gw._query("https://plex.tv/api/servers/x/shared_servers", "post", idempotent=False,
                      json={"shared_server": {"invited_id": 11, "library_section_ids": []}})
        self.assertFalse(gw.state.ok)
        self.assertIn("ReadTimeout", gw.state.last_error)

    def test_rejected_token_surfaces_as_auth_error(self):
        fake, gw = make()
        fake.token_valid = False
        with self.assertRaises(PlexAuthError):
            gw.list_libraries()
        self.assertTrue(gw.state.auth_failed)
        fake.token_valid = True
        self.assertEqual(len(gw.list_libraries()), 4)  # signs in again on the next call

    def test_user_settings_snapshot(self):
        fake, gw = make()
        gw.list_users()
        self.assertIsNone(gw.user_settings("11"))  # no share: plex.tv doesn't report settings
        fake.shares["11"] = {"id": 9, "sections": {101}}
        self.assertEqual(gw.user_settings("11")["allowSync"], True)

    def test_roster_merges_all_sources(self):
        fake, gw = make()
        fake.shares["11"] = {"id": 9, "sections": {101}}
        users = {u.plex_user_id: u for u in gw.list_users()}
        # alice (share), bob (friend, no share: only in v2), kid (Home only), owner; pending invite left out
        self.assertEqual(set(users), {"1", "11", "12", "13"})
        self.assertTrue(users["1"].is_owner)
        self.assertTrue(users["13"].is_managed)
        self.assertEqual(gw.get_user_library_ids("12"), frozenset())

    def test_friend_without_share_gets_one(self):
        fake, gw = make()
        gw.set_user_libraries("12", ["2"])
        self.assertEqual(fake.calls[-1][0], "post")
        self.assertEqual(gw.get_user_library_ids("12"), frozenset({"2"}))

    def test_invite_posts_like_plexapi_invitefriend(self):
        fake, gw = make()
        fake.users["30"] = {"username": "newbie", "email": "newbie@x.com"}
        gw.invite("newbie@x.com", ["1", "2"])
        method, url, body = fake.calls[-1]
        self.assertEqual((method, url), ("post", f"https://plex.tv/api/servers/{MACHINE}/shared_servers"))
        self.assertEqual(body["shared_server"]["invited_email"], "newbie@x.com")
        self.assertEqual(sorted(body["shared_server"]["library_section_ids"]), [101, 102])
        with self.assertRaises(ValueError):
            gw.invite("x@x.com", [])

    def test_owner_is_never_written(self):
        _fake, gw = make()
        with self.assertRaises(ValueError):
            gw.set_user_libraries("1", ["1"])

    def test_v2_friends_outage_falls_back_to_shared_list(self):
        fake, gw = make()
        fake.shares["11"] = {"id": 9, "sections": {101}}
        fake.friends_v2_fails = True
        gw._retries = 0
        ids = {u.plex_user_id for u in gw.list_users()}
        self.assertEqual(ids, {"1", "11", "13"})  # bob missing, nothing crashed

    def test_unknown_user_still_not_found(self):
        _fake, gw = make()
        from plexapi.exceptions import NotFound

        with self.assertRaises(NotFound):
            gw.get_user_library_ids("4040")


class BatchReadTests(unittest.TestCase):
    def test_one_friends_fetch_for_all_users(self):
        fake, gw = make()
        fake.shares["11"] = {"id": 9, "sections": {101}}
        fake.shares["12"] = {"id": 10, "sections": {102, 103}}
        gw.list_users()  # a full sync runs the inventory first
        fake.users_calls = 0
        shares, errors = gw.get_all_user_library_ids()
        self.assertEqual(fake.users_calls, 1)
        self.assertEqual(shares, {"1": frozenset(), "11": frozenset({"1"}), "12": frozenset({"2", "3"}),
                                  "13": frozenset()})
        self.assertEqual(errors, {})

    def test_failed_user_is_reported_not_fatal(self):
        fake, gw = make()
        gw._retries = 0
        fake.shares["11"] = {"id": 9, "sections": {101}}
        fake.shares["12"] = {"id": 10, "sections": {102}}
        fake.broken_users = {"12"}
        shares, errors = gw.get_all_user_library_ids()
        self.assertEqual(shares["11"], frozenset({"1"}))
        self.assertNotIn("12", shares)
        self.assertIn("ConnectionError", errors["12"])
