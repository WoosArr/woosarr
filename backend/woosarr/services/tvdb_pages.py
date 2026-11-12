"""Exact titles for TVDB name links (P13-07): thetvdb.com/series/the-killing-2011 → TMDB tv 34415.

Most TVDB links carry the title's name, not its number, and a name with a year can still fit
several titles. TVDB's public page for that name lists the title's TMDB and IMDb links and its
TVDB id, so it's read and the first of those it has is used: a TMDB link opens the card directly,
IMDb and TVDB ids go through Seerr's own id search. Without the page (slow, down, changed) the
caller falls back to searching for the name.

Only https://www.thetvdb.com/<series|movies>/<name> is ever fetched, built from the name (never
the pasted link), without following redirects, reading at most MAX_BYTES. Answers are kept for a
day, failures for a few minutes, so a title is fetched about once a day however often it's pasted.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

import requests

from woosarr.services.emoji_library import USER_AGENT
from woosarr.services.media_links import Lookup

log = logging.getLogger("woosarr.tvdb")

PAGE = "https://www.thetvdb.com/{kind}/{slug}"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
MAX_BYTES = 2 * 1024 * 1024
KEEP_SECONDS, KEEP_FAILED_SECONDS, KEEP_MAX = 24 * 3600, 300, 1000
_ID = re.compile(r"TheTVDB\.com (?:Series|Movie) ID</strong>\s*<span>(\d{1,10})</span>")
_TMDB = re.compile(r"https?://(?:www\.)?themoviedb\.org/(movie|tv)/(\d{1,10})\b")
_IMDB = re.compile(r"https?://(?:www\.)?imdb\.com/title/(tt\d{7,10})\b")


def read_page(html: str, media_type: str) -> Lookup | None:
    """The title a TVDB page is about, from its external links (one distinct link of the right
    kind, else it isn't trusted) or its TVDB id. None if the page has none of them."""
    tmdb = {(k, int(i)) for k, i in _TMDB.findall(html) if k == media_type}
    if len(tmdb) == 1:
        ((kind, tmdb_id),) = tmdb
        return Lookup("TVDB", media_type=kind, tmdb_id=tmdb_id)
    imdb = set(_IMDB.findall(html))
    if len(imdb) == 1:
        return Lookup("TVDB", query=f"imdb:{imdb.pop()}", media_type=media_type)
    ids = set(_ID.findall(html))
    if media_type == "tv" and len(ids) == 1:  # TMDB knows TVDB ids of shows only
        return Lookup("TVDB", query=f"tvdb:{int(ids.pop())}", media_type=media_type)
    return None


class TvdbPages:
    def __init__(self, session: Any = None) -> None:
        self._http = session or requests.Session()
        if hasattr(self._http, "headers"):
            self._http.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], tuple[float, Lookup | None]] = OrderedDict()

    def resolve(self, lookup: Lookup, timeout: float) -> Lookup:
        """The exact title for a TVDB name link, or the lookup unchanged (search by name)."""
        if not lookup.title or lookup.media_type not in ("tv", "movie"):
            return lookup
        slug = "-".join(lookup.title.split())
        if not SLUG.match(slug):
            return lookup
        kind = "series" if lookup.media_type == "tv" else "movies"
        key, now = (kind, slug), time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now < hit[0]:
            return hit[1] or lookup
        found, keep = self._fetch(kind, slug, lookup.media_type, timeout)
        with self._lock:
            self._cache[key] = (now + keep, found)
            self._cache.move_to_end(key)
            while len(self._cache) > KEEP_MAX:
                self._cache.popitem(last=False)
        return found or lookup

    def _fetch(self, kind: str, slug: str, media_type: str, timeout: float) -> tuple[Lookup | None, float]:
        url = PAGE.format(kind=kind, slug=slug)
        started = time.monotonic()
        try:
            r = self._http.get(url, timeout=max(timeout, 0.1), allow_redirects=False, stream=True)
            try:
                if r.status_code != 200:
                    log.info("tvdb page %s/%s: HTTP %s, searching by name", kind, slug, r.status_code)
                    return None, KEEP_FAILED_SECONDS if r.status_code != 404 else KEEP_SECONDS
                body = r.raw.read(MAX_BYTES + 1, decode_content=True) if hasattr(r, "raw") else r.content
            finally:
                r.close()
        except requests.RequestException as exc:
            log.warning("tvdb page %s/%s unavailable after %.1f s (%s), searching by name", kind, slug,
                        time.monotonic() - started, type(exc).__name__)
            return None, KEEP_FAILED_SECONDS
        found = read_page(body[:MAX_BYTES].decode("utf-8", "replace"), media_type)
        if found is None:
            log.warning("tvdb page %s/%s has no TMDB, IMDb or TVDB id (page changed?), searching by name", kind, slug)
            return None, KEEP_FAILED_SECONDS
        log.info("tvdb page %s/%s is %s (%.1f s)", kind, slug,
                 f"tmdb {found.media_type} {found.tmdb_id}" if found.tmdb_id else found.query,
                 time.monotonic() - started)
        return found, KEEP_SECONDS
