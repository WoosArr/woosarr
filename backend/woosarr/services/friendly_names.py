"""Keeps core/names.py's id -> name map current: Discord members, roles, channels and server,
and Plex users, read from WooSarr's caches every minute."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from woosarr.core import names
from woosarr.db.models import DiscordMember, DiscordRole, PlexUser

log = logging.getLogger("woosarr.names")
REFRESH_SECONDS = 60


def clean(name: str) -> str:
    return " ".join(name.replace("(", "[").replace(")", "]").split())[:80]


def load(session: Any) -> tuple[dict[str, str], dict[str, str]]:
    discord = {m.discord_user_id: f"@{clean(m.display_name or m.username)}"
               for m in session.scalars(select(DiscordMember))}
    discord.update({r.role_id: f"role {clean(r.name)}" for r in session.scalars(select(DiscordRole))})
    plex = {u.plex_user_id: f"Plex {clean(u.username)}" for u in session.scalars(select(PlexUser))}
    return discord, plex


def from_guild(client: Any) -> dict[str, str]:
    """The server, its channels and the bot itself, when the bot is connected."""
    if client is None or not client.is_ready():
        return {}
    guild = client.get_guild(client.guild_id)
    out: dict[str, str] = {}
    if guild is not None:
        out[str(guild.id)] = f"server {clean(guild.name)}"
        out.update({str(c.id): f"#{clean(c.name)}" for c in guild.channels})
    if client.user is not None:
        out[str(client.user.id)] = f"@{clean(client.user.name)} [bot]"
    return out


def refresh(db: Any, discord_service: Any = None) -> None:
    with db.session() as s:
        discord, plex = load(s)
    if discord_service is not None:
        discord.update(from_guild(getattr(discord_service, "_client", None)))
    names.update(discord=discord, plex=plex)


async def run_refresher(db: Any, discord_service: Any = None) -> None:
    while True:
        try:
            await asyncio.to_thread(refresh, db, discord_service)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("friendly names refresh failed")
        await asyncio.sleep(REFRESH_SECONDS)
