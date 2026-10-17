"""Discord <-> Plex account linking rules (plan D4, D4a, N9).

Pure decision logic plus the one-shot identity capture used by the OAuth path.
Nothing here persists a Plex credential or token.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class Method(str, Enum):
    USERNAME = "username"  # typed in, unverified
    OAUTH = "oauth"  # authenticated to Plex, verified
    ADMIN = "admin"  # created by an admin, treated as verified
    INVITE = "invite"  # accepted a /invite; verified when an admin sent it


@dataclass(frozen=True)
class Link:
    plex_user_id: str
    discord_user_id: str
    method: Method
    verified: bool


class Outcome(str, Enum):
    CREATED = "created"
    UPGRADED = "upgraded"  # same pair, unverified -> verified
    REPLACED = "replaced"  # a verified link superseded someone's unverified claim
    UNCHANGED = "unchanged"
    PLEX_ACCOUNT_TAKEN = "plex_account_taken"
    DISCORD_ALREADY_LINKED = "discord_already_linked"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    link: Link | None = None
    # Discord user whose unverified claim was removed (REPLACED only)
    displaced_discord_user_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            Outcome.CREATED,
            Outcome.UPGRADED,
            Outcome.REPLACED,
            Outcome.UNCHANGED,
        )


def decide_link(
    plex_user_id: str,
    discord_user_id: str,
    method: Method,
    existing_by_plex: Link | None,
    existing_by_discord: Link | None,
) -> Decision:
    """One Plex account <-> one Discord account (D4a).

    A verified (OAuth/admin) link may supersede someone else's unverified claim
    on the same Plex account. A Discord user who already has a link must be
    unlinked by an admin before linking a different Plex account.
    """
    verified = method in (Method.OAUTH, Method.ADMIN)
    new = Link(plex_user_id, discord_user_id, method, verified)

    if existing_by_discord and existing_by_discord.plex_user_id != plex_user_id:
        return Decision(Outcome.DISCORD_ALREADY_LINKED)

    if existing_by_plex is None:
        return Decision(Outcome.CREATED, new)

    if existing_by_plex.discord_user_id == discord_user_id:
        if verified and not existing_by_plex.verified:
            return Decision(Outcome.UPGRADED, new)
        return Decision(Outcome.UNCHANGED, existing_by_plex)

    # Plex account is claimed by a different Discord user.
    if verified and not existing_by_plex.verified:
        return Decision(Outcome.REPLACED, new, existing_by_plex.discord_user_id)
    return Decision(Outcome.PLEX_ACCOUNT_TAKEN)


@dataclass(frozen=True)
class PlexIdentity:
    """What we keep from a Plex sign-in: who they are, never how they proved it."""

    plex_user_id: str
    username: str


def capture_identity_once(
    token: str,
    read_identity: Callable[[str], PlexIdentity],
    invalidate: Callable[[str], None],
) -> PlexIdentity:
    """Use a one-time Plex token to learn the user's identity, then invalidate it.

    Invalidation runs even if reading the identity fails (N9). If invalidation
    itself fails the error is raised so the caller can log it and treat the
    link as not completed; the token is never returned or stored.
    """
    try:
        identity = read_identity(token)
    finally:
        invalidate(token)
    return PlexIdentity(identity.plex_user_id, identity.username)
