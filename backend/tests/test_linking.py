import unittest

from woosarr.linking import (
    Link,
    Method,
    Outcome,
    PlexIdentity,
    capture_identity_once,
    decide_link,
)


def L(plex, discord, method=Method.USERNAME):
    return Link(plex, discord, method, method != Method.USERNAME)


class LinkRuleTests(unittest.TestCase):
    def test_new_username_link_is_unverified(self):
        d = decide_link("p1", "d1", Method.USERNAME, [], None)
        self.assertEqual(d.outcome, Outcome.CREATED)
        self.assertFalse(d.link.verified)

    def test_new_oauth_link_is_verified(self):
        d = decide_link("p1", "d1", Method.OAUTH, [], None)
        self.assertTrue(d.link.verified)

    def test_oauth_upgrades_own_unverified_link(self):
        existing = L("p1", "d1")
        d = decide_link("p1", "d1", Method.OAUTH, [existing], existing)
        self.assertEqual(d.outcome, Outcome.UPGRADED)
        self.assertTrue(d.link.verified)

    def test_repeat_username_link_unchanged(self):
        existing = L("p1", "d1")
        d = decide_link("p1", "d1", Method.USERNAME, [existing], existing)
        self.assertEqual(d.outcome, Outcome.UNCHANGED)

    def test_username_cannot_take_account_linked_by_someone_else(self):
        for family in (False, True):
            d = decide_link("p1", "d2", Method.USERNAME, [L("p1", "d1")], None, family_join=family)
            self.assertEqual(d.outcome, Outcome.PLEX_ACCOUNT_TAKEN)
            d = decide_link("p1", "d2", Method.USERNAME, [L("p1", "d1", Method.OAUTH)], None, family_join=family)
            self.assertEqual(d.outcome, Outcome.PLEX_ACCOUNT_TAKEN)

    def test_verified_link_supersedes_unverified_claims(self):
        for family in (False, True):
            d = decide_link("p1", "d3", Method.OAUTH, [L("p1", "d1"), L("p1", "d2")], None, family_join=family)
            self.assertEqual(d.outcome, Outcome.REPLACED)
            self.assertEqual(d.displaced_discord_user_ids, ("d1", "d2"))
            self.assertEqual(d.link.discord_user_id, "d3")

    def test_verified_member_blocks_oauth_unless_families_may_join(self):
        d = decide_link("p1", "d2", Method.OAUTH, [L("p1", "d1", Method.OAUTH)], None)
        self.assertEqual(d.outcome, Outcome.PLEX_ACCOUNT_TAKEN)

    def test_family_join_by_verified_sign_in_drops_unverified_claims(self):
        existing = [L("p1", "d1", Method.OAUTH), L("p1", "d2")]
        d = decide_link("p1", "d3", Method.OAUTH, existing, None, family_join=True)
        self.assertEqual(d.outcome, Outcome.JOINED)
        self.assertTrue(d.ok)
        self.assertEqual(d.displaced_discord_user_ids, ("d2",))  # d1 (verified) stays
        refused = decide_link("p1", "d3", Method.OAUTH, existing, None, family_join=False)
        self.assertEqual((refused.outcome, refused.displaced_discord_user_ids), (Outcome.PLEX_ACCOUNT_TAKEN, ()))

    def test_discord_user_cannot_link_second_plex_account(self):
        d = decide_link("p2", "d1", Method.USERNAME, [], L("p1", "d1"))
        self.assertEqual(d.outcome, Outcome.DISCORD_ALREADY_LINKED)


class IdentityCaptureTests(unittest.TestCase):
    def test_token_invalidated_after_success_and_not_in_result(self):
        invalidated = []
        identity = capture_identity_once(
            "TOKEN-CANARY",
            read_identity=lambda t: PlexIdentity("42", "alice"),
            invalidate=invalidated.append,
        )
        self.assertEqual(invalidated, ["TOKEN-CANARY"])
        self.assertEqual(identity, PlexIdentity("42", "alice"))
        self.assertNotIn("TOKEN-CANARY", repr(identity))

    def test_token_invalidated_even_if_read_fails(self):
        invalidated = []

        def boom(_t):
            raise RuntimeError("plex down")

        with self.assertRaises(RuntimeError):
            capture_identity_once("TOKEN-CANARY", boom, invalidated.append)
        self.assertEqual(invalidated, ["TOKEN-CANARY"])


if __name__ == "__main__":
    unittest.main()
