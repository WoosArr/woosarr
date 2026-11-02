"""Links pasted into /request (P13-07): title pages on IMDb, TMDB and TVDB, nothing else.

On a phone it's easier to share a title from the IMDb or TMDB app or website (Share → Copy link)
and paste it after /request than to type the title. Only these three sites are recognised; a link
to anywhere else is refused with a note on what works (LinkRejected). Links are only read, never
opened, so a pasted link can't make WooSarr contact anything.

Also accepted, unadvertised: tt1375666, imdb:tt1375666, tmdb:27205, tvdb:81189 (Seerr's own
search prefixes, which is what IMDb and TVDB links are turned into).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

HOW = ("Paste a link to the title's page on IMDb, TMDB or TVDB (in their app or on their website: "
       "Share → Copy link), or type the title.")
# One autocomplete suggestion is at most 100 characters.
HOW_SHORT = "Only IMDb, TMDB or TVDB links work: open the title there, then Share → Copy link"

# A link somewhere in the text ("Check out Inception on IMDb: https://…" from a share sheet).
_LINK = re.compile(r"(?i)(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*")
_LANG = r"(?:[a-z]{2}(?:-[a-z]{2})?/)?"  # imdb.com/de/title/…, themoviedb.org/en-US/movie/…
_IMDB_PATH = re.compile(rf"(?i)^/{_LANG}title/(tt\d{{7,10}})(?:/|$)")
_TMDB_PATH = re.compile(rf"(?i)^/{_LANG}(movie|tv)/(\d{{1,10}})(?:[-/]|$)")
_TVDB_PATH = re.compile(r"(?i)^/(?:dereferrer/)?series/(\d{1,10})/?$")
_TVDB_NAMED = re.compile(r"(?i)^/(?:series|movies)/[^/]+")
_IMDB_ID = re.compile(r"(?i)^(?:imdb:)?(tt\d{7,10})$")
_PREFIXED = re.compile(r"(?i)^(tmdb|tvdb):(\d{1,10})$")
SITES = {"imdb.com": "IMDb", "themoviedb.org": "TMDB", "thetvdb.com": "TVDB"}


@dataclass(frozen=True)
class Lookup:
    site: str  # IMDb | TMDB | TVDB, for messages and logs
    query: str | None = None  # Seerr search text, e.g. "imdb:tt1375666"
    media_type: str | None = None  # movie | tv: a TMDB link names the title exactly
    tmdb_id: int | None = None


class LinkRejected(Exception):
    """The text holds a link WooSarr can't use; the message is safe to show the member."""

    def __init__(self, message: str, host: str) -> None:
        super().__init__(message)
        self.host = host  # for the log


def _site(host: str) -> str | None:
    return next((name for domain, name in SITES.items() if host == domain or host.endswith("." + domain)), None)


def parse(text: str) -> Lookup | None:
    """What the text points at, or None for a plain title. Raises LinkRejected for other links."""
    text = text.strip()
    if m := _IMDB_ID.match(text):
        return Lookup("IMDb", query=f"imdb:{m.group(1).lower()}")
    if m := _PREFIXED.match(text):
        kind = m.group(1).lower()
        return Lookup(kind.upper(), query=f"{kind}:{int(m.group(2))}")
    found = _LINK.search(text)
    if not found:
        return None
    link = found.group(0)
    parts = urlsplit(link if "://" in link else "https://" + link)
    host = (parts.hostname or "").lower().rstrip(".")
    site = _site(host)
    if site is None:
        raise LinkRejected(f"Links to {host[:60] or 'that site'} don't work here. {HOW}", host[:60])
    if site == "IMDb" and (m := _IMDB_PATH.match(parts.path)):
        return Lookup(site, query=f"imdb:{m.group(1).lower()}")
    if site == "TMDB" and (m := _TMDB_PATH.match(parts.path)):
        return Lookup(site, media_type=m.group(1).lower(), tmdb_id=int(m.group(2)))
    if site == "TVDB":
        if m := _TVDB_PATH.match(parts.path):
            return Lookup(site, query=f"tvdb:{int(m.group(1))}")
        tvdb_id = (parse_qs(parts.query).get("id") or [""])[0]
        if tvdb_id.isdigit() and len(tvdb_id) <= 10:
            return Lookup(site, query=f"tvdb:{int(tvdb_id)}")
        if _TVDB_NAMED.match(parts.path):
            raise LinkRejected("That TVDB link has the show's name instead of its number, so it can't be looked "
                               "up. Paste the title's IMDb or TMDB link instead, or type the title.", host)
    raise LinkRejected(f"That {site} link isn't a movie or TV show's page. {HOW}", host)
