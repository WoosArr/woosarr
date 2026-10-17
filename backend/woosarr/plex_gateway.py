"""Boundary between the sync engine and Plex (plan D1b).

Only the interface and an in-memory fake live here. The python-plexapi
implementation is deliberately NOT written yet: P0-03 (verify replace-vs-merge,
last-library removal, filter side effects, managed users against a real server)
gates all Plex write code.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PlexUser:
    plex_user_id: str
    username: str
    email: str | None = None
    is_managed: bool = False


@dataclass(frozen=True)
class PlexLibrary:
    library_id: str  # section key
    title: str
    type: str


class PlexGateway(Protocol):
    def list_users(self) -> list[PlexUser]: ...

    def list_libraries(self) -> list[PlexLibrary]: ...

    def get_user_library_ids(self, plex_user_id: str) -> frozenset[str]:
        """Libraries currently shared with the user, read live."""

    def set_user_libraries(self, plex_user_id: str, library_ids: Iterable[str]) -> None:
        """Write the COMPLETE set. An empty set means remove all shares."""


@dataclass
class FakePlexGateway:
    """In-memory gateway for tests. Records every write for assertions."""

    users: list[PlexUser] = field(default_factory=list)
    libraries: list[PlexLibrary] = field(default_factory=list)
    shares: dict[str, set[str]] = field(default_factory=dict)
    writes: list[tuple[str, frozenset[str]]] = field(default_factory=list)

    def list_users(self) -> list[PlexUser]:
        return list(self.users)

    def list_libraries(self) -> list[PlexLibrary]:
        return list(self.libraries)

    def get_user_library_ids(self, plex_user_id: str) -> frozenset[str]:
        return frozenset(self.shares.get(plex_user_id, set()))

    def set_user_libraries(self, plex_user_id: str, library_ids: Iterable[str]) -> None:
        full = frozenset(library_ids)
        self.writes.append((plex_user_id, full))
        self.shares[plex_user_id] = set(full)
