"""Mirror Plex libraries, users and live shares into the DB (plan P3-03, P3-04).

Works through PlexGateway only, so it runs the same against the fake and the
python-plexapi implementation. Rows that disappear from Plex are flagged
`removed` instead of deleted, so mappings and audit history survive renames
and temporary outages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from woosarr.db.models import Library, LibraryState, PlexUser, utcnow
from woosarr.plex_gateway import PlexGateway

log = logging.getLogger("woosarr.inventory")


@dataclass(frozen=True)
class InventoryResult:
    libraries: int
    libraries_removed: int
    users: int
    users_removed: int


def refresh_libraries(session: Session, gateway: PlexGateway) -> tuple[int, int]:
    live = {lib.library_id: lib for lib in gateway.list_libraries()}
    existing = {row.library_id: row for row in session.scalars(select(Library))}
    for lib_id, lib in live.items():
        row = existing.get(lib_id)
        if row is None:
            session.add(Library(library_id=lib_id, title=lib.title, type=lib.type))
            log.info("library added id=%s title=%s", lib_id, lib.title)
            continue
        if row.title != lib.title:
            log.info("library renamed id=%s %r -> %r", lib_id, row.title, lib.title)
        row.title, row.type, row.removed = lib.title, lib.type, False
        row.synced_at = utcnow()
    removed = 0
    for lib_id, row in existing.items():
        if lib_id not in live and not row.removed:
            row.removed = True
            removed += 1
            log.warning("library no longer on the Plex server id=%s title=%s", lib_id, row.title)
    return len(live), removed


def refresh_users(session: Session, gateway: PlexGateway) -> tuple[int, int]:
    live = {u.plex_user_id: u for u in gateway.list_users()}
    existing = {row.plex_user_id: row for row in session.scalars(select(PlexUser))}
    for uid, user in live.items():
        row = existing.get(uid)
        if row is None:
            session.add(
                PlexUser(plex_user_id=uid, username=user.username, email=user.email, is_managed=user.is_managed,
                         is_owner=user.is_owner)
            )
            continue
        row.username, row.email, row.is_managed, row.removed = user.username, user.email, user.is_managed, False
        row.is_owner = user.is_owner
        row.synced_at = utcnow()
    removed = 0
    for uid, row in existing.items():
        if uid not in live and not row.removed:
            row.removed = True
            removed += 1
            log.info("plex user no longer shared with id=%s username=%s", uid, row.username)
    return len(live), removed


def store_shares(session: Session, plex_user_id: str, library_ids: frozenset[str]) -> None:
    """Record what Plex reported for one user (cache for UI and previews)."""
    session.execute(delete(LibraryState).where(LibraryState.plex_user_id == plex_user_id))
    now = utcnow()
    for lib_id in sorted(library_ids):
        session.add(LibraryState(plex_user_id=plex_user_id, library_id=lib_id, observed_at=now))


def refresh_all(session: Session, gateway: PlexGateway) -> InventoryResult:
    """Libraries and users. Shares are read per user by the sync run, so one
    user's Plex error can't abort the inventory for everyone."""
    libs, libs_removed = refresh_libraries(session, gateway)
    users, users_removed = refresh_users(session, gateway)
    session.flush()
    return InventoryResult(libs, libs_removed, users, users_removed)
