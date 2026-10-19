"""Self-service `/link` (plan D4, D4a, N9, P4-04).

Matches a Discord member's claim to a Plex user WooSarr knows, applies
linking.decide_link, enforces the per-user cooldown, audits every attempt and
queues a sync. No Plex credential or token is ever stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from woosarr.core import app_settings
from woosarr.db.models import AuditLog, DiscordLink, DiscordMember, PlexUser, utcnow
from woosarr.linking import Link, Method, Outcome, PlexIdentity, decide_link
from woosarr.services import audit

log = logging.getLogger("woosarr.link")

ATTEMPT_ACTIONS = ("link_self", "link_failed", "invite_sent", "invite_failed")


@dataclass(frozen=True)
class LinkResult:
    ok: bool
    message: str
    plex_user_id: str | None = None
    displaced_plex_user_id: str | None = None


def cooldown_remaining(session: Session, discord_user_id: str) -> int:
    seconds = app_settings.load(session).link_cooldown_seconds
    if seconds <= 0:
        return 0
    last = session.scalar(
        select(func.max(AuditLog.ts)).where(
            AuditLog.actor == f"discord:{discord_user_id}", AuditLog.action.in_(ATTEMPT_ACTIONS)
        )
    )
    if last is None:
        return 0
    remaining = (last + timedelta(seconds=seconds) - utcnow()).total_seconds()
    return max(0, int(remaining + 0.999))


def _as_link(row: DiscordLink | None) -> Link | None:
    if row is None:
        return None
    return Link(row.plex_user_id, row.discord_user_id, Method(row.method), row.verified)


def _fail(session: Session, discord_user_id: str, reason: str, message: str, **details) -> LinkResult:
    audit.record(session, f"discord:{discord_user_id}", "link_failed", "discord_user", discord_user_id,
                 reason=reason, **details)
    return LinkResult(False, message)


def _apply(session: Session, discord_user_id: str, plex: PlexUser, method: Method) -> LinkResult:
    by_plex = session.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == plex.plex_user_id))
    by_discord = session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id))
    decision = decide_link(plex.plex_user_id, discord_user_id, method, _as_link(by_plex), _as_link(by_discord))
    actor = f"discord:{discord_user_id}"
    if decision.outcome == Outcome.DISCORD_ALREADY_LINKED:
        return _fail(session, discord_user_id, "discord_already_linked",
                     "Your Discord account is already linked to another Plex account. Ask an admin to unlink it first.",
                     plex_user_id=plex.plex_user_id)
    if decision.outcome == Outcome.PLEX_ACCOUNT_TAKEN:
        return _fail(session, discord_user_id, "plex_account_taken",
                     "That Plex account is already linked to someone else. Sign in with Plex to prove it's yours, "
                     "or ask an admin.", plex_user_id=plex.plex_user_id)
    if decision.outcome == Outcome.UNCHANGED:
        audit.record(session, actor, "link_self", "plex_user", plex.plex_user_id, outcome="unchanged", method=method.value)
        return LinkResult(True, f"You're already linked to **{plex.username}**.", plex.plex_user_id)
    link = decision.link
    assert link is not None
    if decision.outcome == Outcome.REPLACED and by_plex is not None:
        session.delete(by_plex)
        session.flush()
    if decision.outcome == Outcome.UPGRADED and by_plex is not None:
        by_plex.method, by_plex.verified, by_plex.linked_at = method.value, True, utcnow()
    else:
        session.add(DiscordLink(plex_user_id=plex.plex_user_id, discord_user_id=discord_user_id,
                                method=method.value, verified=link.verified, linked_by="self"))
    audit.record(session, actor, "link_self", "plex_user", plex.plex_user_id, outcome=decision.outcome.value,
                 method=method.value, verified=link.verified,
                 displaced_discord_user_id=decision.displaced_discord_user_id)
    verified = " (verified)" if link.verified else ""
    return LinkResult(True, f"Linked to Plex account **{plex.username}**{verified}. Your library access updates shortly.",
                      plex.plex_user_id)


def _known_plex_users(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(PlexUser).where(PlexUser.removed.is_(False))) or 0


def username_link_allowed(session: Session, discord_user_id: str) -> bool:
    """Whether this member may link by typed username (settings: everyone, or members of chosen roles)."""
    st = app_settings.load(session)
    if st.username_link_all_members:
        return True
    member = session.get(DiscordMember, discord_user_id)
    return bool(member and member.in_guild and set(st.username_link_role_ids) & set(member.role_ids or ()))


def link_by_username(session: Session, discord_user_id: str, text: str) -> LinkResult:
    """Unverified: the member typed a Plex username or email."""
    if not username_link_allowed(session, discord_user_id):
        return _fail(session, discord_user_id, "username_link_not_allowed",
                     "Your roles don't allow linking by username. Use **Sign in with Plex** in `/link` instead.")
    claim = text.strip()
    if not claim or len(claim) > 255:
        return _fail(session, discord_user_id, "bad_input", "Please enter your Plex username or email.")
    if _known_plex_users(session) == 0:
        return LinkResult(False, "WooSarr hasn't loaded the Plex users yet. Try again in a few minutes.")
    lowered = claim.lower()
    plex = session.scalar(
        select(PlexUser).where(
            PlexUser.removed.is_(False),
            or_(func.lower(PlexUser.username) == lowered, func.lower(PlexUser.email) == lowered),
        )
    )
    if plex is None:
        return _fail(session, discord_user_id, "no_such_plex_user",
                     "No Plex user with that name has access to this server. Check the spelling, or ask an admin.",
                     claim=claim[:100])
    return _apply(session, discord_user_id, plex, Method.USERNAME)


def link_by_identity(session: Session, discord_user_id: str, identity: PlexIdentity) -> LinkResult:
    """Verified: the member signed in to Plex; identity came from a token that is already invalidated."""
    plex = session.get(PlexUser, identity.plex_user_id)
    if (plex is None or plex.removed) and identity.username:
        # Friends without a share may only be known by username (uuid key) so far.
        plex = session.scalar(select(PlexUser).where(
            PlexUser.removed.is_(False), func.lower(PlexUser.username) == identity.username.lower()))
    if plex is None or plex.removed:
        return _fail(session, discord_user_id, "not_a_server_user",
                     f"You signed in as **{identity.username}**, but that account doesn't have access to this "
                     "Plex server.", plex_user_id=identity.plex_user_id)
    return _apply(session, discord_user_id, plex, Method.OAUTH)


def current_link(session: Session, discord_user_id: str) -> tuple[DiscordLink, PlexUser] | None:
    link = session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id))
    if link is None:
        return None
    return link, session.get(PlexUser, link.plex_user_id)


def unlink_self(session: Session, discord_user_id: str, policy: str) -> str | None:
    """Remove the member's own link (/unlink). Returns the Plex user id, or None."""
    link = session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id))
    if link is None:
        return None
    plex_user_id = link.plex_user_id
    session.delete(link)
    audit.record(session, f"discord:{discord_user_id}", "unlink_self", "plex_user", plex_user_id, policy=policy)
    return plex_user_id
