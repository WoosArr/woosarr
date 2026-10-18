"""Discord guild cache in the DB, and which Plex users each event affects (P4-02, P4-03).

Pure DB logic: the bot module turns gateway events into these calls (run off
the event loop) and queues syncs for the returned Plex user ids.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from woosarr.db.models import DiscordLink, DiscordMember, DiscordRole, utcnow

log = logging.getLogger("woosarr.discord")


@dataclass(frozen=True)
class RoleData:
    role_id: str
    name: str
    color: int = 0
    position: int = 0
    managed: bool = False


@dataclass(frozen=True)
class MemberData:
    discord_user_id: str
    username: str
    display_name: str | None
    role_ids: tuple[str, ...]


def _linked_plex_id(session: Session, discord_user_id: str) -> str | None:
    return session.scalar(select(DiscordLink.plex_user_id).where(DiscordLink.discord_user_id == discord_user_id))


def upsert_role(session: Session, role: RoleData) -> None:
    # One atomic statement: Discord often sends role-create and role-update for a
    # new role together, and their handlers run in parallel worker threads, so
    # "look up, then insert" let both insert (UNIQUE constraint, live 2026-10-18).
    values = {"name": role.name, "color": role.color, "position": role.position, "managed": role.managed,
              "deleted": False, "synced_at": utcnow()}
    session.execute(insert(DiscordRole).values(role_id=role.role_id, **values)
                    .on_conflict_do_update(index_elements=[DiscordRole.role_id], set_=values))
    cached = session.identity_map.get(session.identity_key(DiscordRole, role.role_id))
    if cached is not None:
        session.expire(cached)  # the ORM's copy is now stale


def role_deleted(session: Session, role_id: str) -> list[str]:
    """Mark a role deleted; returns linked Plex users who held it (their access may shrink)."""
    row = session.get(DiscordRole, role_id)
    if row is not None:
        row.deleted = True
    affected = []
    for m in session.scalars(select(DiscordMember)):
        if role_id in (m.role_ids or []):
            m.role_ids = [r for r in m.role_ids if r != role_id]
            pid = _linked_plex_id(session, m.discord_user_id)
            if pid:
                affected.append(pid)
    log.info("discord role deleted id=%s affected linked users=%s", role_id, len(affected))
    return affected


def member_changed(session: Session, member: MemberData, in_guild: bool = True) -> str | None:
    """Upsert one member; returns their linked Plex user id if roles or presence changed."""
    row = session.get(DiscordMember, member.discord_user_id)
    roles = sorted(set(member.role_ids))
    if row is None:
        # Insert-or-ignore: a parallel handler (join + role change together) may have
        # inserted this member since the lookup; then fall through and update it.
        created = session.execute(
            insert(DiscordMember).values(discord_user_id=member.discord_user_id, username=member.username,
                                         display_name=member.display_name, role_ids=roles, in_guild=in_guild,
                                         synced_at=utcnow())
            .on_conflict_do_nothing(index_elements=[DiscordMember.discord_user_id])).rowcount
        row = session.scalars(select(DiscordMember).where(DiscordMember.discord_user_id == member.discord_user_id)
                              .execution_options(populate_existing=True)).one()
    else:
        created = 0
    if created:
        changed = True
    else:
        changed = sorted(row.role_ids or []) != roles or row.in_guild != in_guild
        row.username, row.display_name, row.role_ids, row.in_guild = (
            member.username, member.display_name, roles, in_guild)
        row.synced_at = utcnow()
    if not changed:
        return None
    pid = _linked_plex_id(session, member.discord_user_id)
    log.info("discord member changed id=%s roles=%s in_guild=%s linked_plex=%s",
             member.discord_user_id, roles, in_guild, pid)
    return pid


def member_left(session: Session, discord_user_id: str) -> str | None:
    row = session.get(DiscordMember, discord_user_id)
    if row is None or not row.in_guild:
        return None
    row.in_guild = False
    pid = _linked_plex_id(session, discord_user_id)
    log.info("discord member left id=%s linked_plex=%s", discord_user_id, pid)
    return pid


def full_sync(session: Session, roles: Iterable[RoleData], members: Iterable[MemberData]) -> None:
    """Replace the cache with a complete guild snapshot (on connect / reconnect)."""
    role_ids = set()
    for r in roles:
        upsert_role(session, r)
        role_ids.add(r.role_id)
    for row in session.scalars(select(DiscordRole)):
        if row.role_id not in role_ids:
            row.deleted = True
    present = set()
    for m in members:
        member_changed(session, m, in_guild=True)
        present.add(m.discord_user_id)
    for row in session.scalars(select(DiscordMember)):
        if row.discord_user_id not in present:
            row.in_guild = False
    log.info("discord cache synced roles=%s members=%s", len(role_ids), len(present))
