"""Seerr (Overseerr / Jellyseerr / Seerr) API client (plan P13-01).

Findings from Seerr's source and `seerr-api.yml` (develop, 2026-10):
  * `X-Api-Key` authenticates as user 1 (the owner/admin). Adding
    `X-API-User: <id>` acts as that Seerr user instead, so the member's own
    request permissions, quotas and auto-approve rules apply to requests.
  * `GET /api/v1/user` includes `plexId` and `email` only for callers with
    MANAGE_USERS, which the API key (user 1) has.
  * `POST /api/v1/user/import-from-plex {"plexIds": [...]}` creates Seerr
    users for Plex friends of the server owner who have library access.
  * `POST /api/v1/request` answers 201 with the request, 403 for missing
    permission / quota / blocklist, 409 for a duplicate and 202 when no
    seasons are left to request. Errors carry `{"message": ...}`.
  * Search queries must be strictly percent-encoded (Seerr's OpenAPI
    validator rejects raw `!'()*` and similar), so the URL is built by hand.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests

log = logging.getLogger("woosarr.seerr")

TIMEOUT = 15
POSTER = "https://image.tmdb.org/t/p/w342{path}"
TMDB = "https://www.themoviedb.org/{kind}/{id}"
TVDB = "https://thetvdb.com/?tab=series&id={id}"
IMDB = "https://www.imdb.com/title/{id}/"
# Values from Seerr/TMDB that end up in URLs must match these exactly.
POSTER_PATH = re.compile(r"^/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png|webp)$")
IMDB_ID = re.compile(r"^tt\d{5,10}$")

# MediaRequestStatus / MediaStatus in server/constants/media.ts
REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
MEDIA_STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available", 5: "available",
                6: "blocklisted", 7: "deleted"}


class SeerrError(Exception):
    """A Seerr call failed; `message` is safe to show to admins and members."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalize_url(url: str) -> str:
    """http(s)://host[:port][/base] without a trailing slash or /api/v1."""
    url = url.strip().rstrip("/")
    url = url.removesuffix("/api/v1")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise SeerrError("Enter Seerr's address, for example http://10.0.1.10:5055")
    return url


@dataclass(frozen=True)
class Hit:
    media_type: str  # movie | tv
    tmdb_id: int
    title: str
    year: str
    overview: str
    poster: str | None
    status: str  # MEDIA_STATUS value

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.tmdb_id}"

    @property
    def tmdb_url(self) -> str:
        return TMDB.format(kind=self.media_type, id=self.tmdb_id)

    def label(self, limit: int = 100) -> str:
        """"🍿 Title (year) · available" within `limit` characters (Discord's 100 for
        suggestions and menu options). Long titles are shortened; the note is kept."""
        icon = "\N{POPCORN}" if self.media_type == "movie" else "\N{TELEVISION}"
        year = f" ({self.year})" if self.year else ""
        note = {"available": " · available", "partially_available": " · partly available",
                "pending": " · requested", "processing": " · requested"}.get(self.status, "")
        title = self.title
        room = limit - len(icon) - 1 - len(year) - len(note)
        if len(title) > room:
            title = title[: max(room - 1, 1)].rstrip() + "…"
        return f"{icon} {title}{year}{note}"


@dataclass(frozen=True)
class Season:
    number: int
    episodes: int
    status: str


@dataclass(frozen=True)
class Details(Hit):
    seasons: tuple[Season, ...] = field(default=())
    tvdb_id: int | None = None
    imdb_id: str | None = None

    def links(self) -> list[tuple[str, str]]:
        """(label, url) for the title's pages on TMDB, TVDB (shows) and IMDb."""
        out = [("TMDB", self.tmdb_url)]
        if self.tvdb_id:
            out.append(("TVDB", TVDB.format(id=self.tvdb_id)))
        if self.imdb_id:
            out.append(("IMDb", IMDB.format(id=self.imdb_id)))
        return out


def _media_status(info: dict | None) -> str:
    return MEDIA_STATUS.get(int((info or {}).get("status") or 1), "unknown")


def _hit(item: dict) -> Hit | None:
    kind = item.get("mediaType")
    if kind not in ("movie", "tv") or not item.get("id"):
        return None
    title = item.get("title") if kind == "movie" else item.get("name")
    date = str((item.get("releaseDate") if kind == "movie" else item.get("firstAirDate")) or "")
    year = date[:4] if date[:4].isdigit() else ""
    poster = str(item.get("posterPath") or "")
    return Hit(kind, int(item["id"]), str(title or "?"), year, str(item.get("overview") or ""),
               POSTER.format(path=poster) if POSTER_PATH.match(poster) else None, _media_status(item.get("mediaInfo")))


class SeerrClient:
    def __init__(self, base_url: str, api_key: str, session: Any = None, timeout: float = TIMEOUT) -> None:
        self.base_url = normalize_url(base_url)
        self._key = api_key
        self._http = session or requests.Session()
        self._timeout = timeout

    def _call(self, method: str, path: str, *, user_id: int | None = None, body: Any = None,
              timeout: float | None = None, auth: bool = True) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["X-Api-Key"] = self._key
        if user_id is not None:
            headers["X-API-User"] = str(int(user_id))
        started = time.monotonic()
        url = f"{self.base_url}/api/v1{path}"
        log_path = path.split("?", 1)[0]
        try:
            r = self._http.request(method, url, headers=headers, json=body, timeout=timeout or self._timeout,
                                   allow_redirects=False)
        except requests.Timeout:
            log.warning("seerr %s %s timed out", method, log_path)
            raise SeerrError("Seerr didn't answer in time.") from None
        except requests.RequestException as exc:
            log.warning("seerr %s %s failed: %s", method, log_path, type(exc).__name__)
            raise SeerrError(f"Can't reach Seerr ({type(exc).__name__}).") from None
        ms = int((time.monotonic() - started) * 1000)
        try:
            data = r.json() if r.content else None
        except ValueError:
            data = None
        if r.status_code >= 400 or (300 <= r.status_code < 400):
            message = data.get("message") if isinstance(data, dict) else None
            # 404 on a single request: deleted in Seerr; the caller handles it.
            level = logging.INFO if r.status_code == 404 and method == "GET" else logging.WARNING
            log.log(level, "seerr %s %s -> %s (%s ms) %s", method, log_path, r.status_code, ms, (message or "")[:200])
            if r.status_code == 401 or (r.status_code == 403 and user_id is None and path == "/auth/me"):
                raise SeerrError("Seerr rejected the API key (Seerr → Settings → General → API Key).", r.status_code)
            if 300 <= r.status_code < 400:
                raise SeerrError("Seerr redirected the request; use its direct address (no login portal).",
                                 r.status_code)
            raise SeerrError(message or f"Seerr answered HTTP {r.status_code}.", r.status_code)
        log.debug("seerr %s %s -> %s (%s ms)", method, log_path, r.status_code, ms)
        return r.status_code, data

    # ---- connection ---------------------------------------------------------------

    def server_status(self) -> dict:
        _code, data = self._call("GET", "/status", auth=False)
        if not isinstance(data, dict) or "version" not in data:
            raise SeerrError("That address answered, but not like Seerr does. Check the URL and port.")
        return data

    def me(self) -> dict:
        _code, data = self._call("GET", "/auth/me")
        if not isinstance(data, dict) or "id" not in data:
            raise SeerrError("Seerr didn't return the API key's user.")
        return data

    # ---- users ----------------------------------------------------------------------

    def users(self) -> list[dict]:
        out: list[dict] = []
        skip = 0
        while True:
            _code, data = self._call("GET", f"/user?take=100&skip={skip}&sort=created")
            results = (data or {}).get("results") or []
            out.extend(results)
            total = int(((data or {}).get("pageInfo") or {}).get("results") or 0)
            skip += len(results)
            if not results or skip >= total or skip >= 10000:
                return out

    def import_plex_users(self, plex_ids: list[str]) -> int:
        _code, data = self._call("POST", "/user/import-from-plex", body={"plexIds": [str(p) for p in plex_ids]})
        created = (data or {}).get("createdUsers") if isinstance(data, dict) else data
        return len(created or [])

    # ---- media ----------------------------------------------------------------------

    def search(self, query: str, timeout: float | None = None) -> list[Hit]:
        _code, data = self._call("GET", f"/search?query={quote(query.strip(), safe='')}&page=1&language=en",
                                 timeout=timeout)
        hits = [_hit(i) for i in (data or {}).get("results") or []]
        return [h for h in hits if h is not None]

    def details(self, media_type: str, tmdb_id: int) -> Details:
        if media_type not in ("movie", "tv"):
            raise SeerrError("Unknown media type.")
        _code, data = self._call("GET", f"/{media_type}/{int(tmdb_id)}")
        hit = _hit(dict(data or {}, mediaType=media_type))
        if hit is None:
            raise SeerrError("Seerr doesn't know that title.", 404)
        seasons: tuple[Season, ...] = ()
        if media_type == "tv":
            info = (data or {}).get("mediaInfo") or {}
            state = {int(s.get("seasonNumber", -1)): MEDIA_STATUS.get(int(s.get("status") or 1), "unknown")
                     for s in info.get("seasons") or []}
            seasons = tuple(Season(int(s["seasonNumber"]), int(s.get("episodeCount") or 0),
                                   state.get(int(s["seasonNumber"]), "unknown"))
                            for s in (data or {}).get("seasons") or [] if int(s.get("seasonNumber", 0)) > 0)
        ext = (data or {}).get("externalIds") or {}
        tvdb = ext.get("tvdbId") or ((data or {}).get("mediaInfo") or {}).get("tvdbId")
        tvdb = tvdb if isinstance(tvdb, int) or (isinstance(tvdb, str) and tvdb.isdigit()) else None
        imdb = ext.get("imdbId") or (data or {}).get("imdbId")
        return Details(**hit.__dict__, seasons=seasons, tvdb_id=int(tvdb) if tvdb and media_type == "tv" else None,
                       imdb_id=str(imdb) if imdb and IMDB_ID.match(str(imdb)) else None)

    # ---- requests -------------------------------------------------------------------

    def create_request(self, user_id: int, media_type: str, tmdb_id: int,
                       seasons: list[int] | None = None) -> tuple[int, dict | None]:
        """(HTTP status, request). 202 = nothing left to request (no request created)."""
        body: dict[str, Any] = {"mediaType": media_type, "mediaId": int(tmdb_id), "is4k": False}
        if media_type == "tv":
            body["seasons"] = sorted(seasons) if seasons else "all"
        code, data = self._call("POST", "/request", user_id=user_id, body=body)
        return code, data if isinstance(data, dict) else None

    def get_request(self, request_id: int) -> dict:
        _code, data = self._call("GET", f"/request/{int(request_id)}")
        return data or {}


def request_state(request: dict) -> str:
    """One status for members: pending, approved, declined, failed, partially_available, available."""
    req = REQUEST_STATUS.get(int(request.get("status") or 1), "pending")
    if req in ("declined", "failed"):
        return req
    media = _media_status(request.get("media"))
    if media in ("available", "partially_available"):
        return media
    if req == "completed":
        return "available"
    return req
