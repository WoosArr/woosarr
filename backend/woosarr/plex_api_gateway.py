"""PlexGateway backed by python-plexapi and plex.tv (plan P3-00, P3-05, P3-06).

Everything goes through plex.tv with the server owner's token, so WooSarr
doesn't need a LAN connection to the Plex server itself.

Findings from reading python-plexapi 4.18 (docs/plex-notes.md):
  * `MyPlexServerShare.sections()` returns every library with a `shared` flag;
    only `shared=True` ones are shared. `Section.key` is the server's library
    section key, the same key LibrarySection uses.
  * plex.tv addresses libraries by its own section id; the mapping
    key -> id comes from GET https://plex.tv/api/servers/{machineId}.
  * `updateFriend(removeSections=True)` silently does nothing for a user who
    already has a share unless `sections` is also passed, and section names
    are matched by (ambiguous) title. So writes call the same endpoints
    directly: PUT the complete id list, POST for a user without a share, and
    DELETE the share to remove the last library (N6).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from woosarr.plex_gateway import PlexLibrary, PlexUser

log = logging.getLogger("woosarr.plex")

PLEXSERVERS = "https://plex.tv/api/servers/{machineId}"
FRIENDINVITE = "https://plex.tv/api/servers/{machineId}/shared_servers"
FRIENDSERVERS = "https://plex.tv/api/servers/{machineId}/shared_servers/{serverId}"

T = TypeVar("T")


class PlexAuthError(Exception):
    """The stored Plex token was rejected; the admin must sign in again."""


@dataclass
class ConnectionState:
    ok: bool | None = None
    last_ok_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    auth_failed: bool = False


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    # plexapi raises BadRequest("(429) too_many_requests; ...") / 5xx the same way.
    return isinstance(exc, BadRequest) and str(exc)[:5] in ("(429)", "(500)", "(502)", "(503)", "(504)")


class PlexApiGateway:
    def __init__(
        self,
        account_factory: Callable[[], Any],
        machine_id: str,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._account_factory = account_factory
        self.machine_id = machine_id
        self._account: Any = None
        self._lock = threading.Lock()
        self._section_ids: dict[str, int] = {}
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self.state = ConnectionState()

    # ---- plumbing -----------------------------------------------------------

    def account(self) -> Any:
        with self._lock:
            if self._account is None:
                self._account = self._call(self._account_factory, idempotent=True)
            return self._account

    def _call(self, fn: Callable[[], T], idempotent: bool) -> T:
        """Run one Plex call with retries/backoff and connection-state tracking."""
        attempt = 0
        while True:
            try:
                result = fn()
            except Unauthorized as exc:
                self._record_error(f"Plex rejected the token: {exc}", auth=True)
                self._account = None  # plain assignment; next call signs in again
                raise PlexAuthError("Plex sign-in expired or was revoked; sign in again") from exc
            except Exception as exc:
                # Writes are retried only if the request can't have reached Plex.
                retryable = _transient(exc) if idempotent else isinstance(exc, requests.ConnectionError)
                if retryable and attempt < self._retries:
                    delay = self._backoff * (2**attempt)
                    attempt += 1
                    log.warning("plex call failed (%s: %s); retry %s/%s in %.0fs",
                                type(exc).__name__, exc, attempt, self._retries, delay)
                    self._sleep(delay)
                    continue
                self._record_error(f"{type(exc).__name__}: {exc}")
                raise
            self.state.ok = True
            self.state.last_ok_at = time.time()
            self.state.auth_failed = False
            return result

    def _record_error(self, message: str, auth: bool = False) -> None:
        self.state.ok = False
        self.state.last_error = message
        self.state.last_error_at = time.time()
        self.state.auth_failed = auth
        log.error("plex: %s", message)

    def _query(self, url: str, method: str = "get", idempotent: bool = True, **kwargs: Any) -> Any:
        acct = self.account()
        fn = getattr(acct._session, method)
        return self._call(lambda: acct.query(url, fn, **kwargs), idempotent=idempotent)

    # ---- reads --------------------------------------------------------------

    def list_libraries(self) -> list[PlexLibrary]:
        data = self._query(PLEXSERVERS.format(machineId=self.machine_id))
        server = data[0]
        libs, ids = [], {}
        for elem in server:
            key, sid = elem.attrib.get("key"), elem.attrib.get("id")
            if not key or not sid:
                continue
            ids[str(key)] = int(sid)
            libs.append(PlexLibrary(str(key), elem.attrib.get("title", ""), elem.attrib.get("type", "")))
        self._section_ids = ids
        log.debug("plex libraries machine=%s %s", self.machine_id, [(lib.library_id, lib.title) for lib in libs])
        return libs

    def _users(self) -> list[Any]:
        acct = self.account()
        return self._call(acct.users, idempotent=True)

    def list_users(self) -> list[PlexUser]:
        out = []
        for u in self._users():
            name = u.username or u.title or str(u.id)
            out.append(PlexUser(str(u.id), name, u.email or None, is_managed=bool(u.home and not u.email)))
        return out

    def _user(self, plex_user_id: str) -> Any:
        for u in self._users():
            if str(u.id) == str(plex_user_id):
                return u
        raise NotFound(f"Plex user {plex_user_id} is not a friend or Home user of this account")

    def _share(self, user: Any) -> Any | None:
        shares = [s for s in user.servers if s.machineIdentifier == self.machine_id]
        return shares[0] if shares else None

    def get_user_library_ids(self, plex_user_id: str) -> frozenset[str]:
        """Read live: re-fetches the user and their share every call (R2 drift check)."""
        user = self._user(plex_user_id)
        share = self._share(user)
        if share is None:
            return frozenset()
        sections = self._call(share.sections, idempotent=True)
        shared = frozenset(str(s.key) for s in sections if s.shared)
        log.debug("plex shares user=%s share=%s all=%s shared=%s",
                  plex_user_id, share.id, getattr(share, "allLibraries", None), sorted(shared))
        return shared

    def user_settings(self, plex_user_id: str) -> dict[str, Any]:
        """Per-friend settings that a share update must not change (P0-03 d)."""
        u = self._user(plex_user_id)
        return {k: getattr(u, k, None) for k in (
            "allowSync", "allowCameraUpload", "allowChannels",
            "filterMovies", "filterTelevision", "filterMusic", "filterPhotos", "filterAll",
        )}

    # ---- writes -------------------------------------------------------------

    def _plex_section_ids(self, library_ids: Iterable[str]) -> list[int]:
        wanted = sorted(set(library_ids), key=lambda k: (len(k), k))
        if any(k not in self._section_ids for k in wanted):
            self.list_libraries()
        missing = [k for k in wanted if k not in self._section_ids]
        if missing:
            raise ValueError(f"libraries not on the Plex server: {missing}")
        return [self._section_ids[k] for k in wanted]

    def set_user_libraries(self, plex_user_id: str, library_ids: Iterable[str]) -> None:
        """Write the COMPLETE set (N6). Empty removes the share entirely."""
        wanted = frozenset(library_ids)
        user = self._user(plex_user_id)
        share = self._share(user)
        headers = {"Content-Type": "application/json"}
        if not wanted:
            if share is None:
                return
            log.info("plex: removing all libraries user=%s share=%s", plex_user_id, share.id)
            self._query(FRIENDSERVERS.format(machineId=self.machine_id, serverId=share.id), "delete",
                        idempotent=False, headers=headers)
            return
        section_ids = self._plex_section_ids(wanted)
        if share is not None:
            log.info("plex: set libraries user=%s share=%s keys=%s", plex_user_id, share.id, sorted(wanted))
            self._query(
                FRIENDSERVERS.format(machineId=self.machine_id, serverId=share.id), "put", idempotent=False,
                json={"server_id": self.machine_id, "shared_server": {"library_section_ids": section_ids}},
                headers=headers,
            )
        else:
            log.info("plex: share server with user=%s keys=%s", plex_user_id, sorted(wanted))
            self._query(
                FRIENDINVITE.format(machineId=self.machine_id), "post", idempotent=False,
                json={"server_id": self.machine_id,
                      "shared_server": {"library_section_ids": section_ids, "invited_id": int(user.id)}},
                headers=headers,
            )
