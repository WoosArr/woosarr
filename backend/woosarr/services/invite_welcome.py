"""Welcome DM when a Plex invite is accepted (P4-24).

The message is built from up to four admin-editable parts (Settings → Linking → Invite welcome
message); the optional ones only appear when they apply:

* main text: always.
* verify: the link is unverified (the member invited themselves), so `/link` → Sign in with Plex.
* verify_needed: also unverified, and a setting needs a verified link ({features} says which).
* find_server: the member has libraries, so tell them where to find the server.

An empty optional part is left out. Placeholders in braces are filled in (PLACEHOLDERS); member,
Plex and library names are escaped so they can't add Discord formatting or mentions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.db.models import (
    DiscordLink,
    DiscordMember,
    Library,
    LibraryState,
    PlexInvite,
    PlexUser,
    utcnow,
)
from woosarr.services.discord_text import safe

if TYPE_CHECKING:  # app_settings validates the texts with this module
    from woosarr.core.app_settings import AppSettings

log = logging.getLogger("woosarr.invite")

DEFAULT_TEXT = ("**You're on the Plex server 🎉**\n"
                "Your invite was accepted and your Discord account is now linked to the Plex account "
                "**{plex_username}**. You can see **{libraries}**.")
DEFAULT_VERIFY = ("**One more step:** run `/link` here and tap **Sign in with Plex** to confirm the Plex account "
                  "is really yours. Your Plex password is never shared; WooSarr only reads your username.")
DEFAULT_VERIFY_NEEDED = "You'll need this to {features}."
DEFAULT_FIND_SERVER = "Can't find the libraries? Open Plex and pick **{server}** from the server list."

PLACEHOLDERS = {
    "member": "the member's Discord name",
    "plex_username": "their Plex account",
    "libraries": "the libraries they can see, e.g. Movies, TV and Disney",
    "server": "the Plex server's name",
    "features": "what needs a verified link, e.g. request movies and shows with /request",
    "request_command": "the request command, e.g. /request",
}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
MAX_PART = 1500
MAX_MESSAGE = 2000  # Discord's limit for one message
WELCOME_HOURS = 24  # invites accepted longer ago than this are never welcomed (e.g. after an upgrade)


def unknown_placeholders(text: str) -> list[str]:
    return sorted({m for m in _PLACEHOLDER.findall(text) if m not in PLACEHOLDERS})


def render(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template).strip()


def join_words(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def needs_verified(settings: AppSettings, requests_on: bool) -> list[str]:
    """What an unverified member can't do under the current settings, as phrases for {features}."""
    out = []
    if requests_on and settings.request_require_verified:
        out.append(f"request movies and shows with `/{settings.request_command_name}`")
    if settings.ignore_unverified_links:
        out.append("have your library access follow your Discord roles")
    return out


def compose(settings: AppSettings, values: dict[str, str], *, verified: bool, has_libraries: bool,
            features: list[str]) -> str:
    """The message: the main text plus whichever optional parts apply."""
    values = {**values, "features": join_words(features),
              "request_command": f"/{settings.request_command_name}"}
    parts = [settings.invite_welcome_text]
    if not verified:
        parts.append(settings.invite_welcome_verify_text)
        if features:
            parts.append(settings.invite_welcome_verify_needed_text)
    if has_libraries:
        parts.append(settings.invite_welcome_find_server_text)
    text = "\n\n".join(p for p in (render(x, values) for x in parts) if p)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + "…"


@dataclass(frozen=True)
class Welcome:
    invite_id: int
    discord_user_id: str
    verified: bool
    values: dict[str, str]
    has_libraries: bool


def due(session: Session, server_name: str | None) -> list[Welcome]:
    """Accepted, linked invites from the last day that haven't been welcomed yet."""
    since = utcnow() - timedelta(hours=WELCOME_HOURS)
    out = []
    for inv in session.scalars(select(PlexInvite).where(PlexInvite.status == "accepted",
                                                        PlexInvite.welcomed_at.is_(None),
                                                        PlexInvite.resolved_at >= since)):
        link = session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == inv.discord_user_id,
                                                        DiscordLink.plex_user_id == inv.plex_user_id))
        plex = session.get(PlexUser, inv.plex_user_id) if inv.plex_user_id else None
        if link is None or plex is None:  # linked to someone else, or unlinked since: nothing to say
            inv.welcomed_at = utcnow()
            log.info("invite %s: no welcome message (the invite didn't end in a link)", inv.id)
            continue
        titles = sorted(session.scalars(
            select(Library.title).join(LibraryState, LibraryState.library_id == Library.library_id)
            .where(LibraryState.plex_user_id == plex.plex_user_id, Library.removed.is_(False))), key=str.lower)
        member = session.get(DiscordMember, inv.discord_user_id)
        out.append(Welcome(inv.id, inv.discord_user_id, link.verified, {
            "member": safe(member.username if member else "there", 100),
            "plex_username": safe(plex.username, 100),
            "libraries": join_words([safe(t, 100) for t in titles]) or "no libraries yet",
            "server": safe(server_name or "the server", 100),
        }, bool(titles)))
    return out


def mark(session: Session, invite_id: int) -> None:
    inv = session.get(PlexInvite, invite_id)
    if inv is not None:
        inv.welcomed_at = utcnow()
