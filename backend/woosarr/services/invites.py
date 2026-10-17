"""/invite: send a Plex invite for a Discord member and link them once accepted.

Libraries come from the target member's Discord roles (exempt libraries are
never granted by roles). Members may invite themselves if they hold a role in
`invite_role_ids`; Discord admins (Manage Server) may invite for any member.
Accepted invites are matched by email/username during full syncs and linked
(verified when an admin sent the invite).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from woosarr.core import app_settings
from woosarr.db.models import DiscordLink, DiscordMember, PlexInvite, PlexUser, utcnow
from woosarr.services import audit, link_service
from woosarr.services.access import load_snapshot
from woosarr.sync.engine import UserState, plan_for_user

log = logging.getLogger("woosarr.invite")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._%+\-@]{2,255}$")


@dataclass(frozen=True)
class InviteResult:
    ok: bool
    message: str
    invite_id: int | None = None


def _fail(session: Session, actor: str, target: str, reason: str, message: str, **details: Any) -> InviteResult:
    audit.record(session, actor, "invite_failed", "discord_user", target, reason=reason, **details)
    return InviteResult(False, message)


def libraries_for_member(session: Session, discord_user_id: str) -> frozenset[str]:
    """What the member's roles would grant a brand-new Plex user."""
    member = session.get(DiscordMember, discord_user_id)
    if member is None or not member.in_guild:
        return frozenset()
    snap = load_snapshot(session)
    state = UserState(plex_user_id="(invite)", linked=True, role_ids=frozenset(member.role_ids))
    plan = plan_for_user(state, snap.role_map, snap.exempt)
    return plan.target if plan else frozenset()


def create(
    session: Session,
    gateway: Any,
    *,
    actor_discord_id: str,
    actor_is_admin: bool,
    target_discord_id: str,
    identifier: str,
) -> InviteResult:
    settings = app_settings.load(session)
    actor = f"discord:{actor_discord_id}"
    ident = identifier.strip()
    if gateway is None or not hasattr(gateway, "invite"):
        return InviteResult(False, "Plex isn't connected yet. Ask an admin.")
    if settings.dry_run:
        return _fail(session, actor, target_discord_id, "dry_run",
                     "WooSarr is in dry-run, so it can't send Plex invites yet. Ask an admin.")
    if not _IDENTIFIER.match(ident):
        return _fail(session, actor, target_discord_id, "bad_input", "Enter the Plex account's email or username.")
    for_self = target_discord_id == actor_discord_id
    if not actor_is_admin:
        if not for_self:
            return _fail(session, actor, target_discord_id, "not_admin",
                         "Only server admins can invite someone else.")
        member = session.get(DiscordMember, actor_discord_id)
        allowed = set(settings.invite_role_ids)
        if not allowed or member is None or not allowed & set(member.role_ids):
            return _fail(session, actor, target_discord_id, "role_not_allowed",
                         "Your roles don't allow inviting yourself to the Plex server.")
    wait = link_service.cooldown_remaining(session, actor_discord_id)
    if wait:
        return InviteResult(False, f"Please wait {wait} s before trying again.")
    if session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == target_discord_id)):
        return _fail(session, actor, target_discord_id, "already_linked",
                     "That Discord account is already linked to a Plex account.")
    if session.scalar(select(PlexInvite).where(PlexInvite.discord_user_id == target_discord_id,
                                               PlexInvite.status == "pending")):
        return _fail(session, actor, target_discord_id, "already_pending",
                     "There's already a pending invite for that member. Accept it on plex.tv, or ask an admin.")
    lowered = ident.lower()
    known = session.scalar(select(PlexUser).where(
        PlexUser.removed.is_(False),
        or_(func.lower(PlexUser.username) == lowered, func.lower(PlexUser.email) == lowered)))
    if known is not None:
        return _fail(session, actor, target_discord_id, "already_on_server",
                     f"**{known.username}** is already on this Plex server. Use `/link` instead.")
    libs = libraries_for_member(session, target_discord_id)
    if not libs:
        return _fail(session, actor, target_discord_id, "no_libraries",
                     "That member's Discord roles don't give access to any Plex library yet.")
    try:
        gateway.invite(ident, libs)
    except Exception as exc:  # noqa: BLE001 - Plex's reason goes back to the member and the audit log
        log.warning("plex invite for %s failed: %s: %s", ident, type(exc).__name__, exc)
        return _fail(session, actor, target_discord_id, "plex_error",
                     "Plex didn't accept that invite. Check the email or username and try again.",
                     error=f"{type(exc).__name__}: {str(exc)[:200]}")
    inv = PlexInvite(identifier=ident, discord_user_id=target_discord_id,
                     invited_by=f"discord{'-admin' if actor_is_admin else ''}:{actor_discord_id}",
                     verified=actor_is_admin, library_ids=sorted(libs))
    session.add(inv)
    session.flush()
    audit.record(session, actor, "invite_sent", "discord_user", target_discord_id, invite_id=inv.id,
                 identifier=ident, libraries=sorted(libs), by_admin=actor_is_admin)
    return InviteResult(True, f"Invite sent to **{ident}**. Accept it from the Plex email or at "
                              "https://app.plex.tv, and WooSarr links the account automatically.", inv.id)


def match_pending(session: Session) -> list[str]:
    """Link accepted invites (called after each inventory refresh). Returns linked Plex ids."""
    linked = []
    for inv in list(session.scalars(select(PlexInvite).where(PlexInvite.status == "pending"))):
        ident = inv.identifier.lower()
        plex = session.scalar(select(PlexUser).where(
            PlexUser.removed.is_(False), PlexUser.is_owner.is_(False),
            or_(func.lower(PlexUser.username) == ident, func.lower(PlexUser.email) == ident)))
        if plex is None:
            continue
        inv.status, inv.plex_user_id, inv.resolved_at = "accepted", plex.plex_user_id, utcnow()
        taken = session.scalar(select(DiscordLink).where(or_(DiscordLink.plex_user_id == plex.plex_user_id,
                                                             DiscordLink.discord_user_id == inv.discord_user_id)))
        if taken is not None:
            log.warning("invite %s accepted by %s but a link already exists; not linking", inv.id, plex.username)
            audit.record(session, "system", "invite_accepted", "plex_user", plex.plex_user_id, invite_id=inv.id,
                         linked=False, reason="already linked")
            continue
        session.add(DiscordLink(plex_user_id=plex.plex_user_id, discord_user_id=inv.discord_user_id,
                                method="invite", verified=inv.verified, linked_by=inv.invited_by))
        audit.record(session, "system", "invite_accepted", "plex_user", plex.plex_user_id, invite_id=inv.id,
                     discord_user_id=inv.discord_user_id, linked=True, verified=inv.verified)
        log.info("invite %s accepted by %s; linked to discord %s", inv.id, plex.username, inv.discord_user_id)
        linked.append(plex.plex_user_id)
    session.flush()
    return linked
