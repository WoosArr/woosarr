import unittest

from woosarr.sync.engine import UserState, plan_all, plan_for_user

ROLE_MAP = {
    "movies-role": {"movies"},
    "tv-role": {"tv"},
    "both-role": {"movies", "tv"},
    "anime-role": {"anime"},
}


def user(roles=(), manual=(), current=(), linked=True):
    return UserState(
        plex_user_id="u1",
        linked=linked,
        role_ids=frozenset(roles),
        manual=frozenset(manual),
        current=frozenset(current),
    )


class EngineTests(unittest.TestCase):
    def test_unlinked_user_skipped(self):
        self.assertIsNone(plan_for_user(user(["tv-role"], linked=False), ROLE_MAP, []))

    def test_unlinked_user_excluded_from_plan_all(self):
        users = [user(["tv-role"]), UserState("u2", False, frozenset(["tv-role"]))]
        self.assertEqual(len(plan_all(users, ROLE_MAP, [])), 1)

    def test_role_grants_library(self):
        plan = plan_for_user(user(["movies-role"]), ROLE_MAP, [])
        self.assertEqual(plan.target, {"movies"})
        self.assertEqual(plan.add, {"movies"})
        self.assertFalse(plan.remove)

    def test_roles_are_additive(self):
        plan = plan_for_user(user(["movies-role", "tv-role"]), ROLE_MAP, [])
        self.assertEqual(plan.target, {"movies", "tv"})

    def test_removing_one_role_keeps_library_granted_by_another(self):
        # Had movies-role + both-role; both-role removed. movies stays via movies-role.
        plan = plan_for_user(
            user(["movies-role"], current=["movies", "tv"]), ROLE_MAP, []
        )
        self.assertEqual(plan.target, {"movies"})
        self.assertEqual(plan.remove, {"tv"})

    def test_two_roles_same_library_one_removed_keeps_it(self):
        plan = plan_for_user(user(["both-role"], current=["movies", "tv"]), ROLE_MAP, [])
        self.assertEqual(plan.target, {"movies", "tv"})
        self.assertFalse(plan.changed)

    def test_manual_grant_survives_role_removal(self):
        plan = plan_for_user(
            user([], manual=["movies"], current=["movies"]), ROLE_MAP, []
        )
        self.assertIn("movies", plan.target)
        self.assertFalse(plan.remove)

    def test_manual_grant_added_even_without_role(self):
        plan = plan_for_user(user([], manual=["tv"], current=[]), ROLE_MAP, [])
        self.assertEqual(plan.add, {"tv"})
        self.assertEqual(plan.reasons["tv"], "manual grant")

    def test_exempt_library_currently_shared_stays_after_role_removal(self):
        plan = plan_for_user(user([], current=["movies"]), ROLE_MAP, exempt=["movies"])
        self.assertIn("movies", plan.target)
        self.assertFalse(plan.remove)

    def test_exempt_library_not_added_by_role(self):
        plan = plan_for_user(user(["movies-role"], current=[]), ROLE_MAP, exempt=["movies"])
        self.assertNotIn("movies", plan.target)
        self.assertFalse(plan.add)

    def test_unmanaged_library_is_left_alone(self):
        # "extras" is not mapped by any role, so it must never be removed.
        plan = plan_for_user(user([], current=["extras"]), ROLE_MAP, [])
        self.assertIn("extras", plan.target)
        self.assertFalse(plan.remove)

    def test_external_share_in_managed_universe_removed_without_role(self):
        plan = plan_for_user(user([], current=["anime"]), ROLE_MAP, [])
        self.assertEqual(plan.remove, {"anime"})

    def test_deleted_role_grants_nothing(self):
        plan = plan_for_user(user(["ghost-role"], current=[]), ROLE_MAP, [])
        self.assertFalse(plan.target)

    def test_idempotent(self):
        first = plan_for_user(user(["tv-role"], current=[]), ROLE_MAP, [])
        second = plan_for_user(user(["tv-role"], current=first.target), ROLE_MAP, [])
        self.assertFalse(second.changed)

    def test_empty_target_with_existing_access_flags_remove_all(self):
        plan = plan_for_user(user([], current=["movies"]), ROLE_MAP, [])
        self.assertTrue(plan.remove_all)
        self.assertEqual(plan.remove, {"movies"})

    def test_empty_target_without_existing_access_is_noop(self):
        plan = plan_for_user(user([], current=[]), ROLE_MAP, [])
        self.assertFalse(plan.remove_all)
        self.assertFalse(plan.changed)

    def test_target_is_complete_set_not_partial(self):
        plan = plan_for_user(
            user(["tv-role"], current=["movies", "extras"]), ROLE_MAP, exempt=["movies"]
        )
        # movies (exempt) and extras (unmanaged) preserved, tv added.
        self.assertEqual(plan.target, {"movies", "extras", "tv"})

    def test_member_left_server_drops_role_access_keeps_manual_and_exempt(self):
        plan = plan_for_user(
            user([], manual=["tv"], current=["movies", "tv", "anime"]),
            ROLE_MAP,
            exempt=["movies"],
        )
        self.assertEqual(plan.target, {"movies", "tv"})
        self.assertEqual(plan.remove, {"anime"})

    def test_role_mapping_edit_adds_library_for_holder(self):
        new_map = {**ROLE_MAP, "tv-role": {"tv", "anime"}}
        plan = plan_for_user(user(["tv-role"], current=["tv"]), new_map, [])
        self.assertEqual(plan.add, {"anime"})


if __name__ == "__main__":
    unittest.main()
