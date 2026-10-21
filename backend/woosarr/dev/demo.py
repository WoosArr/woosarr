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
# Discord-sized role ids (snowflakes are 17-20 digits), so the demo exercises real-length ids.
MOVIE_NIGHT = "1184392045718302721"
SERIES_FANS = "1184392162903261244"
ANIME_CLUB = "1184392288517390357"
SUPPORTERS = "1184392411049463868"
MODERATORS = "1184392519735279716"
ROLES = [
    (MOVIE_NIGHT, "Movie Night", 0x9B59B6, 5),
    (SERIES_FANS, "Series Fans", 0x3498DB, 4),
    (ANIME_CLUB, "Anime Club", 0xE67E22, 3),
    (SUPPORTERS, "Supporters", 0xF1C40F, 2),
    (MODERATORS, "Moderators", 0x2ECC71, 1),
]
# Discord-sized member ids, too.
ALICE = "748291035561803777"
BOB = "748291188204560415"
CAROL = "748291317952876544"
ERIN = "748291442378301460"
FRANK = "748291569020018699"
MEMBERS = [
    (ALICE, "alice_d", "Alice", [MOVIE_NIGHT, SERIES_FANS]),
    (BOB, "bobby", "Bob", [MOVIE_NIGHT]),
    (CAROL, "carol.c", "Carol", [ANIME_CLUB, SUPPORTERS]),
    (ERIN, "erin_e", "Erin", []),
    (FRANK, "frank", "Frank", [SERIES_FANS]),
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
