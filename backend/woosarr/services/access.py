"""Build sync-engine inputs from the DB and explain access (plan section 6, P7-01).

The engine (sync/engine.py) stays pure; this module is the only place that
knows how links, the Discord member cache, manual grants, exemptions and the
leave-server policy turn into UserState values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core.app_settings import AppSettings
from woosarr.db.models import (
    DiscordLink,
    DiscordMember,
    Library,
    LibraryState,
    ManualGrant,
    PlexUser,
    RoleLibrary,
)
from woosarr.sync.engine import UserState, managed_universe


@dataclass(frozen=True)
class Snapshot:
    """Everything the engine needs, read once from the DB."""

    role_map: dict[str, frozenset[str]]
    exempt: frozenset[str]
    links: dict[str, list[DiscordLink]]  # plex_user_id -> linked members (several for a family account)
    members: dict[str, DiscordMember]  # discord_user_id -> member
    manual: dict[str, frozenset[str]]  # plex_user_id -> library ids
    cached_shares: dict[str, frozenset[str]]  # plex_user_id -> last observed
    plex_user_ids: list[str]
    owner_ids: frozenset[str] = frozenset()

    @property
    def universe(self) -> frozenset[str]:
        return managed_universe(self.role_map, self.exempt)


def load_snapshot(session: Session) -> Snapshot:
    role_map: dict[str, set[str]] = defaultdict(set)
    for row in session.scalars(select(RoleLibrary)):
        role_map[row.role_id].add(row.library_id)
    manual: dict[str, set[str]] = defaultdict(set)
    for row in session.scalars(select(ManualGrant)):
        manual[row.plex_user_id].add(row.library_id)
    shares: dict[str, set[str]] = defaultdict(set)
    for row in session.scalars(select(LibraryState)):
        shares[row.plex_user_id].add(row.library_id)
    return Snapshot(
        role_map={k: frozenset(v) for k, v in role_map.items()},
        exempt=frozenset(session.scalars(select(Library.library_id).where(Library.is_exempt.is_(True)))),
        links=_links_by_plex(session),
        members={row.discord_user_id: row for row in session.scalars(select(DiscordMember))},
        manual={k: frozenset(v) for k, v in manual.items()},
        cached_shares={k: frozenset(v) for k, v in shares.items()},
        plex_user_ids=list(
            session.scalars(select(PlexUser.plex_user_id).where(PlexUser.removed.is_(False)))
        ),
        owner_ids=frozenset(session.scalars(select(PlexUser.plex_user_id).where(PlexUser.is_owner.is_(True)))),
    )


def _links_by_plex(session: Session) -> dict[str, list[DiscordLink]]:
    out: dict[str, list[DiscordLink]] = defaultdict(list)
    for row in session.scalars(select(DiscordLink).order_by(DiscordLink.linked_at, DiscordLink.id)):
        out[row.plex_user_id].append(row)
    return dict(out)


def counted_links(snap: Snapshot, plex_user_id: str, settings: AppSettings,
                  exclude: frozenset[str] = frozenset()) -> list[DiscordLink]:
    """The account's linked members that count (unverified ones not, if ignore_unverified_links)."""
    return [link for link in snap.links.get(plex_user_id, [])
            if link.discord_user_id not in exclude and (link.verified or not settings.ignore_unverified_links)]


def account_roles(snap: Snapshot, plex_user_id: str, settings: AppSettings,
                  exclude: frozenset[str] = frozenset()) -> tuple[bool, frozenset[str]] | None:
    """(linked, union of roles) for a Plex account, or None if policy says leave it alone.

    Members still in the Discord server give their roles. A member who left gives
    nothing under leave_policy drop_role_access, and their last known roles under
    keep_all ("leave their access as it is"). If every member has left and the
    policy is keep_all, the account is left alone entirely.
    """
    links = counted_links(snap, plex_user_id, settings, exclude)
    if not links:
        return False, frozenset()
    roles: set[str] = set()
    present = False
    for link in links:
        member = snap.members.get(link.discord_user_id)
        if member is not None and member.in_guild:
            present = True
            roles.update(member.role_ids)
        elif member is not None and settings.leave_policy == "keep_all":
            roles.update(member.role_ids)
    if not present and settings.leave_policy == "keep_all":
        return None
    return True, frozenset(roles)


def user_state(
    snap: Snapshot, plex_user_id: str, settings: AppSettings, current: frozenset[str]
) -> UserState | None:
    """UserState for one Plex user, or None if policy says leave them alone."""
    if plex_user_id in snap.owner_ids:
        return None  # the owner has every library; never planned or written
    found = account_roles(snap, plex_user_id, settings)
    if found is None:
        return None
    linked, roles = found
    return UserState(
        plex_user_id=plex_user_id,
        linked=linked,
        role_ids=roles,
        manual=snap.manual.get(plex_user_id, frozenset()),
        current=current,
    )


def library_sources(
    snap: Snapshot, state: UserState | None, current: frozenset[str]
) -> dict[str, list[str]]:
    """Why each library the user has (or will get) is there: role/manual/exempt/external."""
    out: dict[str, list[str]] = {}
    roles = state.role_ids if state else frozenset()
    manual = state.manual if state else frozenset()
    role_libs = {lib for r in roles for lib in snap.role_map.get(r, ())}
    universe = snap.universe
    for lib in sorted(current | role_libs | manual):
        sources: list[str] = []
        if lib in snap.exempt and lib in current:
            sources.append("exempt")
        if lib in role_libs and lib in universe:
            sources.append("role")
        if lib in manual:
            sources.append("manual")
        if not sources and lib in current:
            sources.append("external")
        if sources:
            out[lib] = sources
    return out


def role_libraries(snap: Snapshot, roles: frozenset[str]) -> frozenset[str]:
    """Managed libraries these roles give (exempt libraries are never role-granted)."""
    universe = snap.universe
    return frozenset(lib for r in roles for lib in snap.role_map.get(r, ()) if lib in universe)


def role_holders(snap: Snapshot) -> Mapping[str, list[str]]:
    """role_id -> plex_user_ids of linked accounts where a member in the server holds it."""
    holders: dict[str, list[str]] = defaultdict(list)
    for plex_id, links in snap.links.items():
        for link in links:
            member = snap.members.get(link.discord_user_id)
            if member and member.in_guild:
                for role in member.role_ids:
                    if plex_id not in holders[role]:
                        holders[role].append(plex_id)
    return holders
