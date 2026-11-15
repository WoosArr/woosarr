"""Helping unlinked members link or get a Plex invite (P14-08).

Two ways in, both off by default:
- Settings → Linking → *DM unlinked members who react* (`reaction_link_dm`): a member without a
  linked Plex account who reacts to a reaction post gets a DM, at most once every DM_EVERY_DAYS.
- Reaction post options *link button* / *invite button*: buttons under the post (each on its own
  row) that answer the member privately.

Both use the same buttons (discord_bot._HelpView). The decisions live here so they can be tested
without Discord.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import app_settings, kv
from woosarr.db.models import DiscordLink, DiscordMember, utcnow
from woosarr.services.discord_text import safe

K_SENT = "link_help.dm_sent"  # {discord_user_id: ISO time of the last DM}
DM_EVERY_DAYS = 7

LINK_LABEL = "Link my Plex account"
INVITE_LABEL = "Get a Plex invite"


def buttons_key(post) -> str:
    """Which buttons a reaction post shows, as stored in buttons_posted ("link,invite", "" for none)."""
    return ",".join(name for name, on in (("link", post.link_button), ("invite", post.invite_button)) if on)


def is_linked(session: Session, discord_user_id: str) -> bool:
    return session.scalar(select(DiscordLink.id).where(DiscordLink.discord_user_id == discord_user_id)) is not None


def may_invite_self(session: Session, discord_user_id: str) -> bool:
    """Whether the member's roles allow /invite for themselves (admins are checked separately:
    that needs their Discord permissions)."""
    st = app_settings.load(session)
    member = session.get(DiscordMember, discord_user_id)
    if member is None or not member.in_guild:
        return False
    return st.invite_all_members or bool(set(st.invite_role_ids) & set(member.role_ids or []))


def claim_dm(session: Session, discord_user_id: str, now: datetime | None = None) -> bool:
    """True (and remembered) when the member may get the DM now: on, not linked, and not DMed in the
    last DM_EVERY_DAYS. Claimed before sending, so quick repeated reactions send one DM."""
    if not app_settings.load(session).reaction_link_dm or is_linked(session, discord_user_id):
        return False
    now = now or utcnow()
    cutoff = now - timedelta(days=DM_EVERY_DAYS)
    sent: dict[str, str] = {}
    for uid, at in (kv.get(session, K_SENT) or {}).items():
        try:
            when = datetime.fromisoformat(at)
        except (TypeError, ValueError):
            continue
        if when > cutoff:
            sent[uid] = at  # older entries are forgotten
    if discord_user_id in sent:
        return False
    sent[discord_user_id] = now.isoformat()
    kv.put(session, K_SENT, sent)
    return True


def dm_text(server: str | None, role_names: list[str], can_invite: bool) -> str:
    """The DM to an unlinked member who just reacted for a role."""
    roles = ", ".join(f"**{safe(n, 100)}**" for n in role_names) or "a role"
    where = f" in **{safe(server, 100)}**" if server else ""
    invite = (f"• New to the Plex server? Press **{INVITE_LABEL}** and enter your Plex email or username."
              if can_invite else "• New to the Plex server? Ask a server admin for an invite.")
    return (f"You reacted to get {roles}{where}. Roles only unlock Plex libraries once your Discord account is "
            "linked to your Plex account.\n"
            f"• Already on the Plex server? Press **{LINK_LABEL}**.\n"
            f"{invite}\n"
            "You can also use `/link` and `/invite` in the server.")


def not_allowed_to_invite() -> str:
    return ("Your roles don't allow inviting yourself to the Plex server. Ask a server admin for an invite. "
            f"Already on the Plex server? Press **{LINK_LABEL}** instead.")


def already_linked() -> str:
    return "Your Discord account is already linked to a Plex account. Use `/unlink` first to change it."
