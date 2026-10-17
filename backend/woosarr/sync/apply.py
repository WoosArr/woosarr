"""Apply a Plan through the PlexGateway, with dry-run (plan D12, N6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from woosarr.plex_gateway import PlexGateway
from woosarr.sync.engine import Plan

log = logging.getLogger("woosarr.sync")


@dataclass(frozen=True)
class ApplyResult:
    plex_user_id: str
    applied: bool  # a write was made
    dry_run: bool
    skipped_reason: str | None = None


def apply_plan(gateway: PlexGateway, plan: Plan, dry_run: bool) -> ApplyResult:
    if not plan.changed:
        return ApplyResult(plan.plex_user_id, applied=False, dry_run=dry_run, skipped_reason="no change")

    for lib in sorted(plan.add):
        log.info("user=%s add library=%s reason=%s dry_run=%s", plan.plex_user_id, lib, plan.reasons.get(lib), dry_run)
    for lib in sorted(plan.remove):
        log.info("user=%s remove library=%s reason=%s dry_run=%s", plan.plex_user_id, lib, plan.reasons.get(lib), dry_run)

    if dry_run:
        return ApplyResult(plan.plex_user_id, applied=False, dry_run=True, skipped_reason="dry run")

    # Re-read right before writing so we never clobber a change made in Plex
    # since the plan was computed (risk R2). If it drifted, skip; the next
    # reconcile recomputes from fresh state.
    expected_current = (plan.target - plan.add) | plan.remove
    live = gateway.get_user_library_ids(plan.plex_user_id)
    if live != expected_current:
        log.warning("user=%s share state changed since planning; skipping write", plan.plex_user_id)
        return ApplyResult(plan.plex_user_id, applied=False, dry_run=False, skipped_reason="state drifted")

    # Always the complete target set; empty means "remove all shares".
    gateway.set_user_libraries(plan.plex_user_id, plan.target)
    return ApplyResult(plan.plex_user_id, applied=True, dry_run=False)
