"""Demo data: a fake Plex server and Discord cache for UI work and UI tests.

Enabled with WOOSARR_DEMO=true. It never talks to real Plex or Discord, and the
dashboard shows that demo mode is on.
"""

from __future__ import annotations

from sqlalchemy import select

from woosarr.db.models import DiscordMember, DiscordRole
from woosarr.db.session import Database
from woosarr.plex_gateway import FakePlexGateway, PlexLibrary, PlexUser

LIBRARIES = [
    PlexLibrary("1", "Movies", "movie"),
    PlexLibrary("2", "TV Shows", "show"),
    PlexLibrary("3", "Anime", "show"),
    PlexLibrary("4", "4K Movies", "movie"),
    PlexLibrary("5", "Music", "artist"),
    PlexLibrary("6", "Family Videos", "movie"),
]
USERS = [
    PlexUser("1001", "alice", "alice@example.com"),
    PlexUser("1002", "bob", "bob@example.com"),
    PlexUser("1003", "carol", "carol@example.com"),
    PlexUser("1004", "dave", None, is_managed=True),
    PlexUser("1005", "erin", "erin@example.com"),
]
SHARES = {"1001": {"1", "2"}, "1002": {"1", "6"}, "1003": {"5"}, "1004": {"6"}, "1005": set()}
ROLES = [
    ("9001", "Movie Night", 0x9B59B6, 5),
    ("9002", "Series Fans", 0x3498DB, 4),
    ("9003", "Anime Club", 0xE67E22, 3),
    ("9004", "Supporters", 0xF1C40F, 2),
    ("9005", "Moderators", 0x2ECC71, 1),
]
MEMBERS = [
    ("50001", "alice_d", "Alice", ["9001", "9002"]),
    ("50002", "bobby", "Bob", ["9001"]),
    ("50003", "carol.c", "Carol", ["9003", "9004"]),
    ("50004", "erin_e", "Erin", []),
    ("50005", "frank", "Frank", ["9002"]),
]


def make_gateway() -> FakePlexGateway:
    return FakePlexGateway(
        users=list(USERS), libraries=list(LIBRARIES), shares={k: set(v) for k, v in SHARES.items()}
    )


def seed_discord(db: Database) -> None:
    with db.session() as s:
        if s.scalar(select(DiscordRole).limit(1)) is not None:
            return
        for role_id, name, color, pos in ROLES:
            s.add(DiscordRole(role_id=role_id, name=name, color=color, position=pos))
        for uid, username, display, roles in MEMBERS:
            s.add(DiscordMember(discord_user_id=uid, username=username, display_name=display, role_ids=roles))
        # A role that was deleted in Discord, so the Roles page can show "Remove".
        s.add(DiscordRole(role_id="900000000000000099", name="Old Movie Night", color=0x95A5A6, position=0,
                          deleted=True))
