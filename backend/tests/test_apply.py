import unittest

from woosarr.plex_gateway import FakePlexGateway
from woosarr.sync.apply import apply_plan
from woosarr.sync.engine import UserState, plan_for_user

ROLE_MAP = {"tv-role": {"tv"}, "movies-role": {"movies"}}


def make(current, roles, manual=()):
    gw = FakePlexGateway(shares={"u1": set(current)})
    state = UserState("u1", True, frozenset(roles), frozenset(manual), frozenset(current))
    return gw, plan_for_user(state, ROLE_MAP, [])


class ApplyTests(unittest.TestCase):
    def test_writes_complete_set_preserving_existing_access(self):
        gw, plan = make(current=["movies", "extras"], roles=["movies-role", "tv-role"])
        result = apply_plan(gw, plan, dry_run=False)
        self.assertTrue(result.applied)
        self.assertEqual(gw.shares["u1"], {"movies", "extras", "tv"})
        self.assertEqual(gw.writes[-1][1], frozenset({"movies", "extras", "tv"}))

    def test_dry_run_makes_no_writes(self):
        gw, plan = make(current=[], roles=["tv-role"])
        result = apply_plan(gw, plan, dry_run=True)
        self.assertFalse(result.applied)
        self.assertEqual(gw.writes, [])
        self.assertEqual(gw.shares["u1"], set())

    def test_no_change_is_not_written(self):
        gw, plan = make(current=["tv"], roles=["tv-role"])
        result = apply_plan(gw, plan, dry_run=False)
        self.assertFalse(result.applied)
        self.assertEqual(gw.writes, [])

    def test_remove_all_writes_empty_set(self):
        gw, plan = make(current=["tv"], roles=[])
        self.assertTrue(plan.remove_all)
        apply_plan(gw, plan, dry_run=False)
        self.assertEqual(gw.writes[-1], ("u1", frozenset()))

    def test_second_run_is_idempotent(self):
        gw, plan = make(current=[], roles=["tv-role"])
        apply_plan(gw, plan, dry_run=False)
        state = UserState("u1", True, frozenset(["tv-role"]), frozenset(), frozenset(gw.shares["u1"]))
        again = plan_for_user(state, ROLE_MAP, [])
        self.assertFalse(apply_plan(gw, again, dry_run=False).applied)
        self.assertEqual(len(gw.writes), 1)

    def test_skips_write_if_plex_changed_after_planning(self):
        gw, plan = make(current=["movies"], roles=["movies-role", "tv-role"])
        gw.shares["u1"].add("surprise")  # changed in Plex after planning
        result = apply_plan(gw, plan, dry_run=False)
        self.assertFalse(result.applied)
        self.assertEqual(result.skipped_reason, "state drifted")
        self.assertEqual(gw.writes, [])


if __name__ == "__main__":
    unittest.main()
