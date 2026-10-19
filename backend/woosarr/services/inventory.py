"""Mirror Plex libraries, users and live shares into the DB (plan P3-03, P3-04).

Works through PlexGateway only, so it runs the same against the fake and the
python-plexapi implementation. Rows that disappear from Plex are flagged
`removed` instead of deleted, so mappings and audit history survive renames
and temporary outages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    Library,
    LibraryState,
    ManualGrant,
    PlexUser,
    utcnow,
)
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


def rekey_user(session: Session, old_id: str, new_id: str) -> None:
    """Move a Plex user (and its link, grants, observed shares) to a new id.

    Friends known only from the community list are keyed "uuid:<id>"; once
    plex.tv reports their numeric account id they're re-keyed so nothing that
    points at them is lost. If a row with the numeric id already exists (e.g.
    the friend had a share before, lost it, then got one again), the two are
    merged; on conflicts the numeric row's data wins.
    """
    old = session.get(PlexUser, old_id)
    if old is None:
        return
    new = session.get(PlexUser, new_id)
    merged = new is not None
    if new is None:
        new = PlexUser(plex_user_id=new_id, username=old.username, email=old.email, is_managed=old.is_managed,
                       is_owner=old.is_owner, thumb=old.thumb)
        session.add(new)
        session.flush()

    # Members move along (an account may be shared; a Discord member has one link only, so nobody
    # ends up linked twice). If the other record already has members, only verified links join
    # them, as with /link: an unverified claim can't attach itself to someone else's account.
    target_has_members = session.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == new_id)) is not None
    for old_link in list(session.scalars(select(DiscordLink).where(DiscordLink.plex_user_id == old_id))):
        if target_has_members and not old_link.verified:
            log.warning("plex user %s and %s both linked; dropping the unverified link of discord %s on %s",
                        old_id, new_id, old_link.discord_user_id, old_id)
            session.delete(old_link)
        else:
            old_link.plex_user_id = new_id
    for grant in list(session.scalars(select(ManualGrant).where(ManualGrant.plex_user_id == old_id))):
        if session.get(ManualGrant, (new_id, grant.library_id)) is None:
            session.add(ManualGrant(plex_user_id=new_id, library_id=grant.library_id, granted_by=grant.granted_by,
                                    granted_at=grant.granted_at))
        session.delete(grant)
    session.execute(delete(LibraryState).where(LibraryState.plex_user_id == old_id))  # re-read on this sync
    session.execute(update(AuditLog).where(AuditLog.target_type == "plex_user", AuditLog.target_id == old_id)
                    .values(target_id=new_id))
    session.flush()
    session.delete(old)
    session.flush()
    log.info("plex user re-keyed %s -> %s (%s)%s", old_id, new_id, old.username,
             " (merged into existing row)" if merged else "")


def refresh_users(session: Session, gateway: PlexGateway) -> tuple[int, int]:
    live = {u.plex_user_id: u for u in gateway.list_users()}
    # A friend first seen without a share (uuid key) now reported with a numeric id.
    uuid_rows = {row.username.lower(): row.plex_user_id
                 for row in session.scalars(select(PlexUser).where(PlexUser.plex_user_id.like("uuid:%")))}
    for uid, user in live.items():
        old_id = uuid_rows.get(user.username.lower())
        if old_id and old_id != uid and not uid.startswith("uuid:") and old_id not in live:
            rekey_user(session, old_id, uid)
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
            log.info("plex user gone from the owner's friends/Home list id=%s username=%s", uid, row.username)
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
