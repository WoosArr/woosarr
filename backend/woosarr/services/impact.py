"""Previews: what a change would do to access, from the last observed Plex state.

No Plex calls; uses the share cache (library_state) refreshed by every sync.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import app_settings
from woosarr.core.app_settings import AppSettings
from woosarr.db.models import PlexUser
from woosarr.services.access import Snapshot, load_snapshot, user_state
from woosarr.sync.engine import plan_for_user


def removal_split(snap: Snapshot, settings: AppSettings, plex_user_id: str, current: frozenset[str],
                  wanted: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """(libraries that would be removed, libraries kept because the user's roles grant them)."""
    if plex_user_id in snap.owner_ids:
        return frozenset(), frozenset()
    state = user_state(snap, plex_user_id, settings, current)
    role_libs: set[str] = set()
    if state is not None and state.linked:
        for role in state.role_ids:
            role_libs |= set(snap.role_map.get(role, ())) & snap.universe
    keep = frozenset(wanted & current & role_libs)
    return frozenset((wanted & current) - keep), keep


def removal_preview(session: Session, plex_user_ids: Iterable[str], library_ids: Iterable[str]) -> list[dict]:
    settings = app_settings.load(session)
    snap = load_snapshot(session)
    wanted = frozenset(library_ids)
    names = dict(session.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    out = []
    for uid in plex_user_ids:
        current = snap.cached_shares.get(uid, frozenset())
        remove, keep = removal_split(snap, settings, uid, current, wanted)
        out.append({"plex_user_id": uid, "username": names.get(uid, uid), "removed": sorted(remove),
                    "kept_by_roles": sorted(keep), "not_shared": sorted(wanted - current),
                    "owner": uid in snap.owner_ids})
    return out


def _outcome(snap: Snapshot, settings: AppSettings, uid: str) -> frozenset[str]:
    """What the user's shares end up as after a sync under these settings."""
    current = snap.cached_shares.get(uid, frozenset())
    if settings.dry_run:
        return current
    state = user_state(snap, uid, settings, current)
    plan = plan_for_user(state, snap.role_map, snap.exempt) if state else None
    return plan.target if plan else current


def settings_impact(session: Session, proposed: AppSettings) -> list[dict]:
    """Per user: libraries gained/lost if these settings replaced the current ones."""
    current_settings = app_settings.load(session)
    snap = load_snapshot(session)
    names = dict(session.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    out = []
    for uid in snap.plex_user_ids:
        before = _outcome(snap, current_settings, uid)
        after = _outcome(snap, proposed, uid)
        if before != after:
            out.append({"plex_user_id": uid, "username": names.get(uid, uid),
                        "gain": sorted(after - before), "lose": sorted(before - after)})
    return out
