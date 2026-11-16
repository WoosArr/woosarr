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

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import app_settings, kv
from woosarr.db.models import DiscordLink, DiscordMember, utcnow
from woosarr.services import link_help_text as texts
from woosarr.services.discord_text import safe

if TYPE_CHECKING:
    from woosarr.core.app_settings import AppSettings

K_SENT = "link_help.dm_sent"  # {discord_user_id: ISO time of the last DM}
DM_EVERY_DAYS = 7



def buttons_key(post, st: AppSettings) -> str:
    """Which buttons a reaction post shows and how they look, as stored in buttons_posted:
    "link,invite:<hash of labels and colours>", "" for none. A change means the message needs editing."""
    names = ",".join(name for name, on in (("link", post.link_button), ("invite", post.invite_button)) if on)
    if not names:
        return ""
    look = f"{st.link_button_label}|{st.link_button_style}|{st.invite_button_label}|{st.invite_button_style}"
    return f"{names}:{hashlib.sha256(look.encode()).hexdigest()[:8]}"


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


def dm_text(st: AppSettings, server: str | None, role_names: list[str], can_invite: bool) -> str:
    """The DM to an unlinked member who just reacted for a role: the main text, the invite or no-invite
    part, then the closing (link_help_text.py); empty parts are left out."""
    values = {"roles": ", ".join(f"**{safe(n, 100)}**" for n in role_names) or "a role",
              "server": safe(server or "the server", 100),
              "link_button": safe(st.link_button_label, 100), "invite_button": safe(st.invite_button_label, 100)}
    parts = [st.reaction_link_dm_text,
             st.reaction_link_dm_invite_text if can_invite else st.reaction_link_dm_no_invite_text,
             st.reaction_link_dm_closing_text]
    text = "\n".join(p for p in (texts.render(x, values) for x in parts) if p)
    return text if len(text) <= texts.MAX_MESSAGE else text[:texts.MAX_MESSAGE - 1] + "…"


def not_allowed_to_invite(st: AppSettings) -> str:
    return ("Your roles don't allow inviting yourself to the Plex server. Ask a server admin for an invite. "
            f"Already on the Plex server? Press **{safe(st.link_button_label, 100)}** instead.")


def already_linked() -> str:
    return "Your Discord account is already linked to a Plex account. Use `/unlink` first to change it."
