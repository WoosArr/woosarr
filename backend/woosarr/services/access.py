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
    links: dict[str, DiscordLink]  # plex_user_id -> link
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
        links={row.plex_user_id: row for row in session.scalars(select(DiscordLink))},
        members={row.discord_user_id: row for row in session.scalars(select(DiscordMember))},
        manual={k: frozenset(v) for k, v in manual.items()},
        cached_shares={k: frozenset(v) for k, v in shares.items()},
        plex_user_ids=list(
            session.scalars(select(PlexUser.plex_user_id).where(PlexUser.removed.is_(False)))
        ),
        owner_ids=frozenset(session.scalars(select(PlexUser.plex_user_id).where(PlexUser.is_owner.is_(True)))),
    )


def user_state(
    snap: Snapshot, plex_user_id: str, settings: AppSettings, current: frozenset[str]
) -> UserState | None:
    """UserState for one Plex user, or None if policy says leave them alone."""
    if plex_user_id in snap.owner_ids:
        return None  # the owner has every library; never planned or written
    link = snap.links.get(plex_user_id)
    linked = link is not None and (link.verified or not settings.ignore_unverified_links)
    roles: frozenset[str] = frozenset()
    if linked and link is not None:
        member = snap.members.get(link.discord_user_id)
        if member is None or not member.in_guild:
            if settings.leave_policy == "keep_all":
                return None
            # drop_role_access: no roles; manual + exempt still kept by the engine.
        else:
            roles = frozenset(member.role_ids)
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


def role_holders(snap: Snapshot) -> Mapping[str, list[str]]:
    """role_id -> plex_user_ids of linked users whose Discord member holds it."""
    holders: dict[str, list[str]] = defaultdict(list)
    for plex_id, link in snap.links.items():
        member = snap.members.get(link.discord_user_id)
        if member and member.in_guild:
            for role in member.role_ids:
                holders[role].append(plex_id)
    return holders
