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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from woosarr.plex_gateway import PlexLibrary, PlexUser

log = logging.getLogger("woosarr.plex")

PLEXSERVERS = "https://plex.tv/api/servers/{machineId}"
FRIENDINVITE = "https://plex.tv/api/servers/{machineId}/shared_servers"
FRIENDSERVERS = "https://plex.tv/api/servers/{machineId}/shared_servers/{serverId}"
# plex.tv/api/users (python-plexapi's users()) only lists friends who currently
# have a share (live finding 2026-10-17). Friend lists, tried in order:
#   * clients.plex.tv/api/v2/friends: numeric account ids (plex.tv/api/v2/friends
#     itself answers 410 Gone since 2026)
#   * community.plex.tv GraphQL allFriendsV2 (used by Ombi, Pulsarr): only a
#     UUID-style id + username, so those friends are keyed "uuid:<id>" and shared
#     with by username until plex.tv reports their numeric id.
FRIENDS_SOURCES = ("https://clients.plex.tv/api/v2/friends",)
COMMUNITY_API = "https://community.plex.tv/api"
ALL_FRIENDS_QUERY = "query GetAllFriends { allFriendsV2 { user { id username displayName } createdAt } }"
HOME_USERS = "https://plex.tv/api/home/users"
UUID_PREFIX = "uuid:"

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
        self.parallel_reads = 6  # concurrent share reads against plex.tv
        self._roster: dict[str, PlexUser] = {}
        self._source_problems: dict[str, str | None] = {}
        self.state = ConnectionState()

    # ---- plumbing -----------------------------------------------------------

    def account(self) -> Any:
        with self._lock:
            if self._account is None:
                self._account = self._call(self._account_factory, idempotent=True)
            return self._account

    def _call(self, fn: Callable[[], T], idempotent: bool, optional: bool = False) -> T:
        """Run one Plex call with retries/backoff and connection-state tracking.

        optional=True: a failure doesn't mark the connection as failing (used for
        best-effort sources like the friends lists).
        """
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
                if not optional:
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

    def _query(self, url: str, method: str = "get", idempotent: bool = True, optional: bool = False,
               **kwargs: Any) -> Any:
        acct = self.account()
        fn = getattr(acct._session, method)
        return self._call(lambda: acct.query(url, fn, **kwargs), idempotent=idempotent, optional=optional)

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

    def _friends_rest(self, url: str) -> list[PlexUser]:
        data = self._query(url, headers={"Accept": "application/json"}, optional=True)
        out = []
        for f in data if isinstance(data, list) else []:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            if f.get("status") not in (None, "accepted"):
                continue  # pending invites aren't users yet
            name = f.get("username") or f.get("title") or str(f["id"])
            out.append(PlexUser(str(f["id"]), name, f.get("email") or None, is_managed=bool(f.get("home"))))
        return out

    def _friends_community(self) -> list[PlexUser]:
        data = self._query(COMMUNITY_API, "post", optional=True, json={"query": ALL_FRIENDS_QUERY},
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
            raise ValueError(f"unexpected community API answer: {str(data)[:200]}")
        out = []
        for f in data["data"].get("allFriendsV2") or []:
            user = (f or {}).get("user") or {}
            if not user.get("id") or not user.get("username"):
                continue
            out.append(PlexUser(f"{UUID_PREFIX}{user['id']}", user["username"], None))
        return out

    def _friends(self) -> tuple[list[PlexUser], str]:
        """All accepted friends from the first source that answers; (users, source label)."""
        errors = []
        for url in FRIENDS_SOURCES:
            try:
                return self._friends_rest(url), url.split("//")[1].split("/")[0] + " v2"
            except Exception as exc:  # noqa: BLE001 - try the next source
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        try:
            return self._friends_community(), "community"
        except Exception as exc:  # noqa: BLE001 - reported below; the shared list still works
            errors.append(f"community: {type(exc).__name__}: {exc}")
        raise RuntimeError("; ".join(errors))

    def _note_source(self, name: str, problem: str | None) -> None:
        """Warn when an optional source starts failing, info when it recovers; quiet otherwise."""
        previous = self._source_problems.get(name)
        if problem and problem != previous:
            log.warning("plex: %s", problem)
        elif not problem and previous:
            log.info("plex: %s source works again", name)
        self._source_problems[name] = problem

    def _home_users(self) -> list[PlexUser]:
        data = self._query(HOME_USERS, optional=True)
        out = []
        for elem in data if data is not None else []:
            uid = elem.attrib.get("id")
            if not uid or elem.attrib.get("admin") in ("1", "true"):
                continue  # the admin of the Home is the owner
            name = elem.attrib.get("username") or elem.attrib.get("title") or uid
            restricted = elem.attrib.get("restricted") in ("1", "true")
            out.append(PlexUser(uid, name, elem.attrib.get("email") or None,
                                is_managed=restricted or not elem.attrib.get("email")))
        return out

    def owner(self) -> PlexUser:
        acct = self.account()
        return PlexUser(str(acct.id), acct.username or acct.title or str(acct.id), acct.email or None,
                        is_owner=True)

    def list_users(self) -> list[PlexUser]:
        """Everyone the owner can share with: friends (with or without a share), Home
        users, plus the owner (flagged; WooSarr never changes the owner's access)."""
        roster: dict[str, PlexUser] = {}
        counts: dict[str, int | str] = {}
        for u in self._users():
            name = u.username or u.title or str(u.id)
            roster[str(u.id)] = PlexUser(str(u.id), name, u.email or None, is_managed=bool(u.home and not u.email))
        counts["with_share"] = len(roster)
        by_name = {u.username.lower(): u.plex_user_id for u in roster.values()}

        def add(found: list[PlexUser]) -> int:
            added = 0
            for u in found:
                if u.plex_user_id in roster or u.username.lower() in by_name:
                    continue  # already known (numeric id wins over a uuid key)
                roster[u.plex_user_id] = u
                by_name[u.username.lower()] = u.plex_user_id
                added += 1
            return added

        try:
            friends, source = self._friends()
        except Exception as exc:  # noqa: BLE001 - shared list still works; say so once per change
            counts["friends"] = "unavailable"
            self._note_source("friends", f"no friends list available ({exc}); friends without a share are missing")
        else:
            counts["friends"] = f"{len(friends)} via {source} (+{add(friends)} without a share)"
            self._note_source("friends", None)
        try:
            home = self._home_users()
        except Exception as exc:  # noqa: BLE001 - optional
            counts["home"] = "unavailable"
            self._note_source("home", f"Home users list unavailable ({type(exc).__name__}: {exc})")
        else:
            counts["home"] = f"{len(home)} (+{add(home)})"
            self._note_source("home", None)
        owner = self.owner()
        roster.pop(owner.plex_user_id, None)
        users = [owner, *roster.values()]
        self._roster = {u.plex_user_id: u for u in users}
        log.info("plex users: %s merged=%s (incl. owner)", counts, len(users))
        return users

    def _shared_user(self, plex_user_id: str) -> Any | None:
        """The /api/users entry (with share details), or None for a known user without a share."""
        users = self._users()
        for u in users:
            if str(u.id) == str(plex_user_id):
                return u
        if plex_user_id.startswith(UUID_PREFIX):
            known = self._roster.get(plex_user_id)
            name = known.username.lower() if known else None
            for u in users:
                if name and (u.username or u.title or "").lower() == name:
                    return u  # got a share since; plex.tv now reports the numeric id
        if plex_user_id not in self._roster:
            self.list_users()
        if plex_user_id not in self._roster:
            raise NotFound(f"Plex user {plex_user_id} is not a friend or Home user of this account")
        return None

    def _check_not_owner(self, plex_user_id: str) -> None:
        if str(self.account().id) == str(plex_user_id):
            raise ValueError("the server owner always has every library; WooSarr never changes it")

    def _share(self, user: Any) -> Any | None:
        shares = [s for s in user.servers if s.machineIdentifier == self.machine_id]
        return shares[0] if shares else None

    def get_user_library_ids(self, plex_user_id: str) -> frozenset[str]:
        """Read live: re-fetches the user and their share every call (R2 drift check)."""
        user = self._shared_user(plex_user_id)
        share = self._share(user) if user is not None else None
        if share is None:
            return frozenset()
        sections = self._call(share.sections, idempotent=True)
        shared = frozenset(str(s.key) for s in sections if s.shared)
        log.debug("plex shares user=%s share=%s all=%s shared=%s",
                  plex_user_id, share.id, getattr(share, "allLibraries", None), sorted(shared))
        return shared

    def get_all_user_library_ids(self) -> tuple[dict[str, frozenset[str]], dict[str, str]]:
        """Every user's shares with ONE friends-list fetch plus parallel share reads.

        Per-user reads re-fetch the whole friends list each time (O(n^2) on big
        servers: ~0.5 s/user live). Full reconciles use this instead; writes still
        re-read the one user live right before writing (drift check).
        """
        users = self._users()
        shares: dict[str, frozenset[str]] = {}
        errors: dict[str, str] = {}
        todo = []
        for u in users:
            share = self._share(u)
            if share is None:
                shares[str(u.id)] = frozenset()
            else:
                todo.append((str(u.id), share))

        def read(item: tuple[str, Any]) -> tuple[str, frozenset[str] | None, str | None]:
            uid, share = item
            try:
                sections = self._call(share.sections, idempotent=True)
                return uid, frozenset(str(s.key) for s in sections if s.shared), None
            except Exception as exc:  # noqa: BLE001 - reported per user; that user is retried alone
                log.warning("plex share read failed user=%s: %s: %s", uid, type(exc).__name__, exc)
                return uid, None, f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=self.parallel_reads, thread_name_prefix="plex-read") as pool:
            for uid, ids, err in pool.map(read, todo):
                if ids is None:
                    errors[uid] = err or "unknown error"
                else:
                    shares[uid] = ids
        # Friends/Home users without a share aren't in /api/users at all.
        for uid in self._roster:
            if uid not in errors:
                shares.setdefault(uid, frozenset())
        log.debug("plex batch read users=%s shared=%s errors=%s", len(users), len(todo), len(errors))
        return shares, errors

    def user_settings(self, plex_user_id: str) -> dict[str, Any] | None:
        """Per-friend settings that a share update must not change (P0-03 d).

        None for a user without a share: plex.tv only reports these settings
        for users that appear in /api/users.
        """
        u = self._shared_user(plex_user_id)
        if u is None:
            return None
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
        self._check_not_owner(plex_user_id)
        user = self._shared_user(plex_user_id)
        share = self._share(user) if user is not None else None
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
            if plex_user_id.startswith(UUID_PREFIX):
                # Only the username is known (community friends list): invite by it,
                # as python-plexapi's inviteFriend does.
                known = self._roster.get(plex_user_id)
                if known is None:
                    self.list_users()
                    known = self._roster.get(plex_user_id)
                if known is None:
                    raise NotFound(f"Plex user {plex_user_id} is no longer a friend")
                target: dict[str, Any] = {"invited_email": known.username}
            else:
                target = {"invited_id": int(plex_user_id)}
            self._query(
                FRIENDINVITE.format(machineId=self.machine_id), "post", idempotent=False,
                json={"server_id": self.machine_id,
                      "shared_server": {"library_section_ids": section_ids, **target}},
                headers=headers,
            )
