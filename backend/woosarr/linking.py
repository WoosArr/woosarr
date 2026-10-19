"""Discord <-> Plex account linking rules (plan D4, D4a, N9).

Pure decision logic plus the one-shot identity capture used by the OAuth path.
Nothing here persists a Plex credential or token.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
    REPLACED = "replaced"  # a verified sign-in superseded other members' unverified claims
    JOINED = "joined"  # added next to other verified members (a family sharing the account)
    UNCHANGED = "unchanged"
    PLEX_ACCOUNT_TAKEN = "plex_account_taken"
    DISCORD_ALREADY_LINKED = "discord_already_linked"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    link: Link | None = None
    # Discord users whose unverified claims on the Plex account are removed (REPLACED/JOINED)
    displaced_discord_user_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome in (
            Outcome.CREATED,
            Outcome.UPGRADED,
            Outcome.REPLACED,
            Outcome.JOINED,
            Outcome.UNCHANGED,
        )


def decide_link(
    plex_user_id: str,
    discord_user_id: str,
    method: Method,
    existing_on_plex: Sequence[Link],
    existing_by_discord: Link | None,
    family_join: bool = False,
) -> Decision:
    """A Discord member links to one Plex account; a Plex account may have several members (D4a, families).

    - A member who already has a link must be unlinked before linking a different Plex account.
    - Typing a username can't claim an account another member is linked to.
    - Signing in with Plex proves the account: it removes other members' unverified claims. If verified
      members remain, it joins them only when `family_join` is on (setting: verified sign-in may join);
      otherwise it's refused and nothing changes. Admins add members through their own path.
    """
    verified = method in (Method.OAUTH, Method.ADMIN)
    new = Link(plex_user_id, discord_user_id, method, verified)

    if existing_by_discord and existing_by_discord.plex_user_id != plex_user_id:
        return Decision(Outcome.DISCORD_ALREADY_LINKED)

    own = next((x for x in existing_on_plex if x.discord_user_id == discord_user_id), None)
    if own is not None:
        if verified and not own.verified:
            return Decision(Outcome.UPGRADED, new)
        return Decision(Outcome.UNCHANGED, own)

    others = [x for x in existing_on_plex if x.discord_user_id != discord_user_id]
    if not others:
        return Decision(Outcome.CREATED, new)
    if not verified:
        return Decision(Outcome.PLEX_ACCOUNT_TAKEN)
    unverified = tuple(x.discord_user_id for x in others if not x.verified)
    if len(unverified) == len(others):
        return Decision(Outcome.REPLACED, new, unverified)
    if family_join:
        return Decision(Outcome.JOINED, new, unverified)
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
