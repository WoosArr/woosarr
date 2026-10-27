"""Friendly names next to ids in log lines from INFO up (Alerts, Logs page, log files).

"User: 284362693538742273" becomes "User: 284362693538742273 (@rWoot715)". The id stays, so a
line can still be searched for. The names come from WooSarr's own caches (Discord members,
roles, channels and server; Plex users), refreshed every minute by services/friendly_names.py.
Alerts group repeats by message with numbers and these names left out (error_digest.normalize).
"""

from __future__ import annotations

import logging
import re
import threading

_lock = threading.Lock()
_discord: dict[str, str] = {}  # snowflake -> "@member", "role X", "#channel", "server X"
_plex: dict[str, str] = {}  # Plex user id -> "Plex username"

# Discord ids are 17-20 digit snowflakes, wherever they appear (also in discord.py's own messages).
_SNOWFLAKE = re.compile(r"(?<![\w.])(\d{17,20})(?![\w.])(?! \()")
# Plex user ids are short numbers, so only where the message says it's a Plex user.
_PLEX = re.compile(r"\b(plex_user[:=]|plex_user_id=|linked_plex=|user=)(\d{1,12})(?![\w.])(?! \()")
# What annotate() adds, for grouping alerts: "<digits> (<name>)".
ADDED = re.compile(r"(?<=\d) \([^()\n]{1,120}\)")


def update(discord: dict[str, str] | None = None, plex: dict[str, str] | None = None) -> None:
    global _discord, _plex
    with _lock:
        if discord is not None:
            _discord = dict(discord)
        if plex is not None:
            _plex = dict(plex)


def snapshot() -> dict[str, str]:
    """The current Discord id -> name map (members, roles, channels, server, bot)."""
    return _discord


def annotate(text: str) -> str:
    discord_names, plex_names = _discord, _plex  # one consistent snapshot, no lock needed to read
    if discord_names:
        text = _SNOWFLAKE.sub(lambda m: f"{m[1]} ({discord_names[m[1]]})" if m[1] in discord_names else m[1], text)
    if plex_names:
        text = _PLEX.sub(lambda m: f"{m[1]}{m[2]} ({plex_names[m[2]]})" if m[2] in plex_names else m[0], text)
    return text


class NamesFilter(logging.Filter):
    """Adds names to INFO and above before they're written (runs before redaction). DEBUG is
    left alone: it's high-volume Plex/Discord/HTTP detail."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must never break logging
            return True
        named = annotate(message)
        if named != message:
            record.msg, record.args = named, None
        return True
