"""Behaviour every PlexGateway implementation must share (plan P3-00, N6).

GatewayContract runs against FakePlexGateway always, and against the real
python-plexapi gateway only when WOOSARR_PLEX_CONTRACT=1 and a test server is
configured (HUMAN gate P0-03). The real gateway does not exist yet, so that
case skips.
"""

import os
import unittest

from woosarr.plex_gateway import FakePlexGateway, PlexLibrary, PlexUser


class GatewayContract:
    """Mixin: subclasses provide make_gateway() returning (gateway, user_id, library_ids)."""

    def make_gateway(self):
        raise NotImplementedError

    def setUp(self):
        self.gw, self.user, self.libs = self.make_gateway()
        self.assertGreaterEqual(len(self.libs), 3, "contract needs at least 3 libraries")
        self.original = self.gw.get_user_library_ids(self.user)

    def tearDown(self):
        self.gw.set_user_libraries(self.user, self.original)

    def test_read_returns_frozenset_of_known_libraries(self):
        current = self.gw.get_user_library_ids(self.user)
        self.assertIsInstance(current, frozenset)
        known = {lib.library_id for lib in self.gw.list_libraries()}
        self.assertLessEqual(current, known)

    def test_write_replaces_not_merges(self):
        a, b, c = self.libs[:3]
        self.gw.set_user_libraries(self.user, [a, b])
        self.gw.set_user_libraries(self.user, [b, c])
        self.assertEqual(self.gw.get_user_library_ids(self.user), frozenset({b, c}))

    def test_complete_set_round_trips(self):
        self.gw.set_user_libraries(self.user, self.libs)
        self.assertEqual(self.gw.get_user_library_ids(self.user), frozenset(self.libs))

    def test_removing_last_library(self):
        self.gw.set_user_libraries(self.user, self.libs[:1])
        self.gw.set_user_libraries(self.user, [])
        self.assertEqual(self.gw.get_user_library_ids(self.user), frozenset())

    def test_write_is_idempotent(self):
        self.gw.set_user_libraries(self.user, self.libs[:2])
        self.gw.set_user_libraries(self.user, self.libs[:2])
        self.assertEqual(self.gw.get_user_library_ids(self.user), frozenset(self.libs[:2]))

    def test_user_is_listed(self):
        self.assertIn(self.user, {u.plex_user_id for u in self.gw.list_users()})


class FakeGatewayContractTests(GatewayContract, unittest.TestCase):
    def make_gateway(self):
        libs = [PlexLibrary("1", "Movies", "movie"), PlexLibrary("2", "TV", "show"),
                PlexLibrary("3", "Music", "artist")]
        gw = FakePlexGateway(users=[PlexUser("u1", "alice")], libraries=libs, shares={"u1": {"1"}})
        return gw, "u1", [lib.library_id for lib in libs]


@unittest.skipUnless(os.environ.get("WOOSARR_PLEX_CONTRACT") == "1", "real Plex contract run is opt-in")
class PlexApiGatewayContractTests(GatewayContract, unittest.TestCase):
    def make_gateway(self):
        self.skipTest("python-plexapi gateway not written yet (gated by P0-03)")
