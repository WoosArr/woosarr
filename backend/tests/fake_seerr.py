"""A small fake of the Seerr API endpoints WooSarr uses (services/seerr.py).

Shapes and rules follow Seerr's source (develop, 2026-10): the API key acts as
user 1 unless X-API-User names another user; unknown X-API-User -> 403;
/user lists plexId/email for the key; import-from-plex only creates Plex
friends with access; POST /request -> 201 / 403 (permission, quota) / 409
(duplicate) / 202 (no seasons left).
"""

from __future__ import annotations

import itertools
import json
from urllib.parse import parse_qs, unquote, urlsplit

import requests

KEY = "seerr-" + "k" * 26  # assembled; not a real key shape


class FakeResponse:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        return json.loads(self.content)


MOVIES = {
    27205: {"title": "Inception", "releaseDate": "2010-07-15", "overview": "Dreams within dreams.",
            "posterPath": "/inception.jpg"},
    157336: {"title": "Interstellar", "releaseDate": "2014-11-05", "overview": "Space.", "posterPath": None},
}
SHOWS = {
    1396: {"name": "Breaking Bad", "firstAirDate": "2008-01-20", "overview": "Chemistry.",
           "posterPath": "/bb.jpg", "seasons": [0, 1, 2, 3, 4, 5]},
}


class FakeSeerr:
    def __init__(self, base="http://seerr.local:5055"):
        self.base = base
        self.key = KEY
        self.users = {1: {"id": 1, "plexId": 1, "email": "owner@x", "plexUsername": "owner", "displayName": "owner",
                          "can_request": True}}
        self.plex_friends: dict[str, dict] = {}  # plex id -> {"username", "email"} importable
        self.media: dict[tuple[str, int], dict] = {}  # (type, tmdb) -> {"status", "seasons": {n: status}}
        self.requests: dict[int, dict] = {}
        self._ids = itertools.count(100)
        self._users = itertools.count(2)
        self.calls: list[tuple[str, str, dict]] = []
        self.quota_exceeded: set[int] = set()
        self.down = False
        self.search_delay = 0.0

    # --- helpers for tests -----------------------------------------------------
    def add_user(self, plex_id=None, email=None, plex_username=None, can_request=True) -> int:
        uid = next(self._users)
        self.users[uid] = {"id": uid, "plexId": plex_id, "email": email, "plexUsername": plex_username,
                           "displayName": plex_username or email, "can_request": can_request}
        return uid

    def set_request_status(self, rid, status=None, media_status=None):
        if status is not None:
            self.requests[rid]["status"] = status
        if media_status is not None:
            self.requests[rid]["media"]["status"] = media_status

    def session(self):
        return self

    # --- requests.Session interface ----------------------------------------------
    def request(self, method, url, headers=None, json=None, timeout=None, allow_redirects=True):
        if self.down:
            raise requests.ConnectionError("connection refused")
        headers = headers or {}
        parts = urlsplit(url)
        assert url.startswith(self.base + "/api/v1/"), url
        path = parts.path[len(urlsplit(self.base).path) + len("/api/v1"):]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((method, path, {"headers": dict(headers), "json": json, "raw_query": parts.query}))
        if path == "/status" and method == "GET":
            return FakeResponse(200, {"version": "2.7.3", "commitTag": "v2.7.3"})
        user = None
        if headers.get("X-Api-Key") == self.key:
            user = self.users.get(int(headers.get("X-API-User", 1)))
        if user is None:
            return FakeResponse(403, {"status": 403, "error": "You do not have permission to access this endpoint"})
        if path == "/auth/me":
            return FakeResponse(200, {k: v for k, v in user.items() if k != "can_request"})
        if path == "/user" and method == "GET":
            take, skip = int(query.get("take", 20)), int(query.get("skip", 0))
            everyone = [{k: v for k, v in u.items() if k != "can_request"} for u in self.users.values()]
            return FakeResponse(200, {"pageInfo": {"results": len(everyone), "pageSize": take},
                                      "results": everyone[skip:skip + take]})
        if path == "/user/import-from-plex" and method == "POST":
            created = []
            for pid in (json or {}).get("plexIds", []):
                friend = self.plex_friends.get(pid)
                if friend and not any(str(u.get("plexId")) == pid for u in self.users.values()):
                    created.append(self.add_user(int(pid), friend["email"], friend["username"]))
            return FakeResponse(201, {"createdUsers": [{"id": i} for i in created], "refreshedUsers": 0})
        if path == "/search":
            # Seerr's validator rejects reserved characters that aren't percent-encoded.
            raw = parts.query.split("query=", 1)[1].split("&", 1)[0]
            if any(c in raw for c in " !'()*"):
                return FakeResponse(400, {"message": "query must be URL encoded"})
            q = unquote(raw).lower()
            results = [self._movie(i) for i, m in MOVIES.items() if q in m["title"].lower()]
            results += [self._show(i) for i, t in SHOWS.items() if q in t["name"].lower()]
            results.append({"id": 287, "mediaType": "person", "name": "Brad Pitt"})
            return FakeResponse(200, {"page": 1, "totalPages": 1, "totalResults": len(results), "results": results})
        if path.startswith("/movie/"):
            tmdb = int(path.rsplit("/", 1)[1])
            if tmdb not in MOVIES:
                return FakeResponse(404, {"message": "Not found"})
            # Details (not search results) carry externalIds.
            return FakeResponse(200, dict(self._movie(tmdb), externalIds={"imdbId": f"tt{tmdb:07d}"}))
        if path.startswith("/tv/"):
            tmdb = int(path.rsplit("/", 1)[1])
            if tmdb not in SHOWS:
                return FakeResponse(404, {"message": "Not found"})
            show = self._show(tmdb)
            show["seasons"] = [{"seasonNumber": n, "episodeCount": 10} for n in SHOWS[tmdb]["seasons"]]
            show["externalIds"] = {"tvdbId": 81189, "imdbId": "tt0903747"}
            return FakeResponse(200, show)
        if path == "/request" and method == "POST":
            return self._create(user, json or {})
        if path.startswith("/request/") and path.endswith("/retry") and method == "POST":
            rid = int(path.split("/")[2])
            if rid not in self.requests:
                return FakeResponse(404, {"message": "Request not found."})
            if self.requests[rid]["status"] != 4:
                return FakeResponse(409, {"message": "Only failed requests can be retried."})
            # Seerr answers with the request alone (no media), approved again.
            self.requests[rid]["status"] = 2
            return FakeResponse(200, {k: v for k, v in self.requests[rid].items() if k != "media"})
        if path.startswith("/request/") and method == "GET":
            rid = int(path.rsplit("/", 1)[1])
            if rid not in self.requests:
                return FakeResponse(404, {"message": "Request not found."})
            return FakeResponse(200, self.requests[rid])
        return FakeResponse(404, {"message": f"no fake for {method} {path}"})

    def _info(self, kind, tmdb):
        m = self.media.get((kind, tmdb))
        if not m:
            return None
        return {"status": m["status"], "seasons": [{"seasonNumber": n, "status": st}
                                                    for n, st in m.get("seasons", {}).items()]}

    def _movie(self, tmdb):
        m = MOVIES[tmdb]
        return {"id": tmdb, "mediaType": "movie", **m, "mediaInfo": self._info("movie", tmdb)}

    def _show(self, tmdb):
        t = {k: v for k, v in SHOWS[tmdb].items() if k != "seasons"}
        return {"id": tmdb, "mediaType": "tv", **t, "mediaInfo": self._info("tv", tmdb)}

    def _create(self, user, body):
        kind, tmdb = body["mediaType"], int(body["mediaId"])
        if not user["can_request"]:
            return FakeResponse(403, {"message": f"You do not have permission to make {kind} requests."})
        if user["id"] in self.quota_exceeded:
            return FakeResponse(403, {"message": "Movie Quota exceeded."})
        media = self.media.setdefault((kind, tmdb), {"status": 1, "seasons": {}})
        if kind == "movie":
            if media["status"] != 1:
                return FakeResponse(409, {"message": "Request for this media already exists."})
        else:
            all_seasons = [n for n in SHOWS[tmdb]["seasons"] if n > 0]
            wanted = all_seasons if body.get("seasons") == "all" else body.get("seasons", [])
            left = [n for n in wanted if media["seasons"].get(n, 1) == 1]
            if not left:
                return FakeResponse(202, {"message": "No seasons available to request"})
            for n in left:
                media["seasons"][n] = 2
        media["status"] = 2
        rid = next(self._ids)
        auto = user["id"] == 1
        self.requests[rid] = {"id": rid, "status": 2 if auto else 1, "is4k": bool(body.get("is4k")),
                              "media": {"tmdbId": tmdb, "mediaType": kind, "status": 3 if auto else 2},
                              "requestedBy": {"id": user["id"]}}
        return FakeResponse(201, self.requests[rid])
