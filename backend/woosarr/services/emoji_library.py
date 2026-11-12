"""Custom Emoji page: searchable emoji libraries, previews, and images made fit for Discord.

Sources:
- emoji.gg: one JSON index (~2 MB, ~5,500 emoji), downloaded at startup and then daily, cached in
  /config/cache/emoji. NSFW (category 9) is left out; category 8 (Animated) is the GIFs.
- OpenMoji: the latest npm release's data/openmoji.json (version checked at startup and daily, the
  index downloaded only when it changes); images from the GitHub repo through jsDelivr. CC BY-SA 4.0.
- slackmojis: no complete index, so it's searched live (20 results a page), results kept briefly.

The browser never loads third-party images: WooSarr fetches previews itself, only from the source's
own image host, over https, without following redirects, size-capped and only PNG/GIF/JPEG/WebP,
and keeps them in a size-capped disk cache.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw, ImageSequence

from woosarr import __version__

log = logging.getLogger("woosarr.emoji")

SOURCES = ("emojigg", "openmoji", "slackmojis")
LABELS = {"emojigg": "emoji.gg", "openmoji": "OpenMoji", "slackmojis": "slackmojis"}
LICENSES = {
    "emojigg": "Uploaded by the emoji.gg community; check the emoji's page for its terms.",
    "openmoji": "OpenMoji, CC BY-SA 4.0: credit OpenMoji (openmoji.org) where you use them.",
    "slackmojis": "Collected from many sources by slackmojis.com; check the emoji's page for credit.",
}

REFRESH_SECONDS = 24 * 3600
# A restart soon after a download doesn't download again.
STARTUP_MIN_AGE = 3600
RETRY_SECONDS = 3600

EMOJIGG_INDEX = "https://emoji.gg/api/"
EMOJIGG_ANIMATED, EMOJIGG_NSFW = 8, 9
# https://emoji.gg/api/?request=categories (ids are stable).
EMOJIGG_CATEGORIES = {
    1: "Original Style", 2: "TV / Movie", 3: "Meme", 4: "Anime", 5: "Celebrity", 6: "Blobs", 7: "Thinking",
    8: "Animated", 10: "Gaming", 11: "Letters", 12: "Other", 13: "Pepe", 14: "Logos", 15: "Cute", 16: "Utility",
    17: "Animals", 18: "Recolors", 19: "Flags", 20: "Hearts", 21: "Pixel Art", 22: "Reactions", 23: "Aesthetic",
}
OPENMOJI_LATEST = "https://data.jsdelivr.com/v1/packages/npm/openmoji/resolved?specifier=latest"
OPENMOJI_INDEX = "https://cdn.jsdelivr.net/npm/openmoji@{version}/data/openmoji.json"
OPENMOJI_IMAGE = "https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@{version}/color/{size}/{hexcode}.png"
SLACKMOJIS_SEARCH = "https://slackmojis.com/emojis/search.json"
SLACKMOJIS_PAGE_SIZE = 20

# Images are only ever fetched from the source's own host.
IMAGE_HOSTS = {"emojigg": {"cdn3.emoji.gg"}, "openmoji": {"cdn.jsdelivr.net"},
               "slackmojis": {"emojis.slackmojis.com"}}
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4096 * 4096
MAX_FRAMES = 1000
PREVIEW_CACHE_BYTES = 200 * 1024 * 1024

# Discord: 256 KiB per emoji; it shows them at up to 128 px.
DISCORD_MAX_BYTES = 256 * 1024
DISCORD_SIZE = 128
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")

USER_AGENT = f"WooSarr/{__version__} (+https://github.com/WoosArr/woosarr)"


class EmojiLibraryError(Exception):
    """Something the admin should see (bad image, source unreachable, unknown emoji)."""


class _HttpError(EmojiLibraryError):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


# Images the source no longer has (its index can list emoji whose file was removed): not asked for
# again for a day, and left out of search results.
MISSING_STATUSES = {403, 404, 410}
MISSING_SECONDS = 24 * 3600
# Downloads at once per source: a page of previews mustn't open dozens of connections to one host.
MAX_PARALLEL_PER_SOURCE = 6


@dataclass(frozen=True)
class LibraryEmoji:
    source: str
    id: str
    name: str  # suggested Discord name
    title: str
    category: str
    animated: bool | None  # None: not known yet (a slackmojis GIF that may be one frame)
    image: str  # full-size image, what gets added
    preview: str  # thumbnail shown on the page
    page: str  # the emoji's page on the source site
    credit: str = ""
    rank: int = 0  # emoji.gg favourites; higher sorts first
    search: str = field(default="", repr=False, compare=False)


def emoji_name(text: str, fallback: str) -> str:
    """A valid Discord emoji name (2-32 of A-Z a-z 0-9 _) from a title."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")[:32].strip("_")
    return name if len(name) >= 2 else f"emoji_{fallback}"[:32]


def sniff(data: bytes) -> str | None:
    """The image type from its first bytes; None for anything else (SVG, HTML, …)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _open(data: bytes) -> Image.Image:
    if sniff(data) is None:
        raise EmojiLibraryError("not a PNG, GIF, JPEG or WebP image")
    try:
        im = Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - Pillow raises many types for broken files
        raise EmojiLibraryError("the image couldn't be read") from None
    if im.width * im.height > MAX_PIXELS:
        raise EmojiLibraryError(f"the image is too large ({im.width}×{im.height})")
    if getattr(im, "n_frames", 1) > MAX_FRAMES:
        raise EmojiLibraryError("the animation has too many frames")
    return im


def is_animated(data: bytes) -> bool:
    try:
        im = _open(data)
    except EmojiLibraryError:
        return False
    return bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1


def prepare_image(data: bytes) -> tuple[bytes, bool]:
    """(image bytes Discord accepts, animated). Static images become a PNG of at most 128 px;
    animations stay GIFs (as they are when already small enough, otherwise resized frame by frame)."""
    im = _open(data)
    animated = bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
    try:
        if animated:
            if sniff(data) == "image/gif" and len(data) <= DISCORD_MAX_BYTES:
                return data, True
            for size in (DISCORD_SIZE, 96, 64, 48, 32):
                out = _gif(im, size)
                if len(out) <= DISCORD_MAX_BYTES:
                    return out, True
            raise EmojiLibraryError("the animation is over Discord's 256 KB limit even at 32 px")
        for size in (DISCORD_SIZE, 96, 64):
            frame = im.convert("RGBA")
            frame.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            frame.save(buf, "PNG", optimize=True)
            if buf.tell() <= DISCORD_MAX_BYTES:
                return buf.getvalue(), False
        raise EmojiLibraryError("the image is over Discord's 256 KB limit even at 64 px")
    except EmojiLibraryError:
        raise
    except Exception:  # noqa: BLE001
        raise EmojiLibraryError("the image couldn't be converted") from None


def _gif(im: Image.Image, size: int) -> bytes:
    frames, durations = [], []
    for frame in ImageSequence.Iterator(im):
        f = frame.convert("RGBA")
        f.thumbnail((size, size), Image.Resampling.LANCZOS)
        frames.append(f)
        durations.append(frame.info.get("duration", im.info.get("duration", 100)) or 100)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                   loop=im.info.get("loop", 0), disposal=2, optimize=True)
    return buf.getvalue()


def _haystack(*parts: str) -> str:
    return " ".join(p.lower() for p in parts if p)


def _parse_emojigg(raw: Any) -> list[LibraryEmoji]:
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            category = int(x.get("category") or 0)
            image = str(x["image"])
            if category == EMOJIGG_NSFW:
                continue
            title, slug = str(x.get("title") or ""), str(x.get("slug") or "")
            cat = EMOJIGG_CATEGORIES.get(category, "Other")
            out.append(LibraryEmoji(
                source="emojigg", id=str(int(x["id"])), name=emoji_name(title, str(x["id"])), title=title,
                category=cat, animated=category == EMOJIGG_ANIMATED or image.lower().endswith(".gif"),
                image=image, preview=image, page=f"https://emoji.gg/emoji/{slug}" if slug else "https://emoji.gg/",
                credit=str(x.get("submitted_by") or ""), rank=int(x.get("faves") or 0),
                search=_haystack(title, cat)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _parse_openmoji(raw: Any, version: str) -> list[LibraryEmoji]:
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            hexcode = str(x["hexcode"])
            if x.get("skintone") or not re.fullmatch(r"[0-9A-F-]{2,64}", hexcode):
                continue  # skin-tone variants would only repeat the base emoji
            title = str(x.get("annotation") or hexcode)
            group = str(x.get("group") or "")
            out.append(LibraryEmoji(
                source="openmoji", id=hexcode, name=emoji_name(title, hexcode.lower()), title=title,
                category=group.replace("-", " ").title(), animated=False,
                image=OPENMOJI_IMAGE.format(version=version, size="618x618", hexcode=hexcode),
                preview=OPENMOJI_IMAGE.format(version=version, size="72x72", hexcode=hexcode),
                page=f"https://openmoji.org/library/emoji-{hexcode}/", credit=str(x.get("openmoji_author") or ""),
                search=_haystack(title, x.get("emoji", ""), group, x.get("subgroups", ""), x.get("tags", ""),
                                 x.get("openmoji_tags", ""))))
        except (KeyError, TypeError):
            continue
    return out


def _parse_slackmojis(raw: Any) -> list[LibraryEmoji]:
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            image, name = str(x["image_url"]), str(x.get("name") or "")
            ext = urlsplit(image).path.rsplit(".", 1)[-1].lower()
            out.append(LibraryEmoji(
                source="slackmojis", id=str(int(x["id"])), name=emoji_name(name, str(x["id"])), title=name,
                category=str((x.get("category") or {}).get("name") or ""), animated=None if ext == "gif" else False,
                image=image, preview=image, page=f"https://slackmojis.com/emojis/{int(x['id'])}-{name}",
                credit=str(x.get("credit") or ""), search=_haystack(name)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


@dataclass
class _Index:
    items: list[LibraryEmoji] = field(default_factory=list)
    by_id: dict[str, LibraryEmoji] = field(default_factory=dict)
    updated_at: float | None = None  # the index last changed
    checked_at: float | None = None  # last successful check
    failed_at: float | None = None
    version: str | None = None
    digest: str | None = None
    error: str | None = None


class EmojiLibrary:
    def __init__(self, cache_dir: Path, session: requests.Session | None = None, demo: bool = False) -> None:
        self._dir = cache_dir
        self._previews = cache_dir / "previews"
        self._demo = demo
        self._http = session or requests.Session()
        if session is None:
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=MAX_PARALLEL_PER_SOURCE + 2)
            self._http.mount("https://", adapter)
        self._slots = {s: threading.BoundedSemaphore(MAX_PARALLEL_PER_SOURCE) for s in SOURCES}
        self._missing: dict[str, float] = {}  # image url -> when the source said it's gone
        self._http.headers["User-Agent"] = USER_AGENT
        self._indexes: dict[str, _Index] = {"emojigg": _Index(), "openmoji": _Index()}
        self._seen: OrderedDict[str, LibraryEmoji] = OrderedDict()  # slackmojis results, for previews/adding
        self._searches: OrderedDict[tuple[str, int], tuple[float, list[LibraryEmoji], bool]] = OrderedDict()
        self._refresh_lock = threading.Lock()
        self._lock = threading.Lock()
        self._cache_bytes: int | None = None
        if demo:
            self._load_demo()
        else:
            self._load_cached()

    # ---- indexes -------------------------------------------------------------------------------

    def _state_file(self) -> Path:
        return self._dir / "state.json"

    def _load_cached(self) -> None:
        """Last downloaded indexes, so search works at once (and when a source is down)."""
        try:
            state = json.loads(self._state_file().read_text())
        except (OSError, ValueError):
            return
        for source in self._indexes:
            st = state.get(source) or {}
            path = self._dir / f"{source}.json"
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            items = _parse_emojigg(raw) if source == "emojigg" else _parse_openmoji(raw, str(st.get("version")))
            self._set(source, items, updated_at=st.get("updated_at"), checked_at=st.get("checked_at"),
                      version=st.get("version"), digest=st.get("digest"))
        log.info("emoji library: cached indexes loaded emojigg=%d openmoji=%d",
                 len(self._indexes["emojigg"].items), len(self._indexes["openmoji"].items))

    def _save_state(self) -> None:
        state = {s: {"updated_at": i.updated_at, "checked_at": i.checked_at, "version": i.version, "digest": i.digest}
                 for s, i in self._indexes.items()}
        _write_atomic(self._state_file(), json.dumps(state).encode())

    def _set(self, source: str, items: list[LibraryEmoji], **meta: Any) -> None:
        old = self._indexes[source]
        new = _Index(items=items, by_id={i.id: i for i in items}, failed_at=None, error=None,
                     **{k: meta.get(k, getattr(old, k)) for k in ("updated_at", "checked_at", "version", "digest")})
        self._indexes[source] = new  # one assignment: searches see the old or the new index, never half

    def due(self, startup: bool = False, now: float | None = None) -> list[str]:
        now = now or time.time()
        out = []
        for source, idx in self._indexes.items():
            if idx.failed_at and now - idx.failed_at < RETRY_SECONDS:
                continue
            age = now - idx.checked_at if idx.checked_at else None
            if age is None or age >= (STARTUP_MIN_AGE if startup else REFRESH_SECONDS):
                out.append(source)
        return out

    def refresh(self, startup: bool = False, force: bool = False) -> None:
        """Download whichever indexes are due (all of them with force). Failures keep the cached index."""
        if self._demo:
            return
        with self._refresh_lock:
            for source in (list(self._indexes) if force else self.due(startup)):
                try:
                    (self._refresh_emojigg if source == "emojigg" else self._refresh_openmoji)()
                except Exception as exc:  # noqa: BLE001 - one source failing mustn't stop the other
                    idx = self._indexes[source]
                    idx.failed_at, idx.error = time.time(), _describe(exc)
                    log.warning("emoji library: %s index update failed (keeping %d cached emoji): %s",
                                LABELS[source], len(idx.items), idx.error)
            self._save_state()

    def _refresh_emojigg(self) -> None:
        body = self._get(EMOJIGG_INDEX, MAX_INDEX_BYTES)
        digest = hashlib.sha256(body).hexdigest()
        idx, now = self._indexes["emojigg"], time.time()
        if digest == idx.digest and idx.items:
            idx.checked_at, idx.error = now, None
            log.info("emoji library: emoji.gg index unchanged (%d emoji)", len(idx.items))
            return
        items = _parse_emojigg(json.loads(body))
        if not items:
            raise EmojiLibraryError("emoji.gg returned no emoji")
        _write_atomic(self._dir / "emojigg.json", body)
        before = len(idx.items)
        self._set("emojigg", items, updated_at=now, checked_at=now, digest=digest)
        log.info("emoji library: emoji.gg index updated: %d emoji (was %d)", len(items), before)

    def _refresh_openmoji(self) -> None:
        resolved = json.loads(self._get(OPENMOJI_LATEST, 64 * 1024))
        version = str(resolved.get("version") or "")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise EmojiLibraryError(f"jsDelivr returned an unexpected OpenMoji version {version[:40]!r}")
        idx, now = self._indexes["openmoji"], time.time()
        if version == idx.version and idx.items:
            idx.checked_at, idx.error = now, None
            log.info("emoji library: OpenMoji %s is current (%d emoji)", version, len(idx.items))
            return
        body = self._get(OPENMOJI_INDEX.format(version=version), MAX_INDEX_BYTES)
        items = _parse_openmoji(json.loads(body), version)
        if not items:
            raise EmojiLibraryError("the OpenMoji index has no emoji")
        _write_atomic(self._dir / "openmoji.json", body)
        self._set("openmoji", items, updated_at=now, checked_at=now, version=version)
        log.info("emoji library: OpenMoji %s index loaded: %d emoji (was %s)", version, len(items), idx.version)

    def sources(self) -> list[dict[str, Any]]:
        out = []
        for source in SOURCES:
            idx = self._indexes.get(source)
            out.append({"key": source, "label": LABELS[source], "license": LICENSES[source], "live": idx is None,
                        "count": len(idx.items) if idx else None, "updated_at": idx.updated_at if idx else None,
                        "checked_at": idx.checked_at if idx else None, "version": idx.version if idx else None,
                        "error": idx.error if idx else None})
        return out

    # ---- search --------------------------------------------------------------------------------

    def search(self, source: str, q: str, hide_animated: bool, offset: int, limit: int) -> tuple[list[LibraryEmoji], int, bool]:
        """(page of results, total, more pages). slackmojis totals are only what's been fetched so far."""
        if source == "slackmojis":
            return self._search_slackmojis(q, hide_animated, offset, limit)
        idx = self._indexes[source]
        if not idx.items and idx.error:
            raise EmojiLibraryError(f"{LABELS[source]} couldn't be downloaded: {idx.error}")
        terms = q.lower().split()
        gone = self._gone()
        hits = [e for e in idx.items if all(t in e.search for t in terms) and not (hide_animated and e.animated)
                and e.preview not in gone]
        if terms:
            want = "_".join(terms)
            hits.sort(key=lambda e: (e.name.lower() != want, not e.name.lower().startswith(terms[0]), -e.rank))
        elif source == "emojigg":
            hits.sort(key=lambda e: -e.rank)
        return hits[offset:offset + limit], len(hits), offset + limit < len(hits)

    def _search_slackmojis(self, q: str, hide_animated: bool, offset: int, limit: int) -> tuple[list[LibraryEmoji], int, bool]:
        q = " ".join(q.split())[:100]
        if not q:
            return [], 0, False
        # Fetch the source's 20-result pages until this page can be filled (or the results end).
        collected: list[LibraryEmoji] = []
        page, more = 0, True
        while more and len(collected) < offset + limit and page < 25:
            items, more = self._slackmojis_page(q, page)
            gone = self._gone()
            collected += [e for e in items if not (hide_animated and e.animated) and e.preview not in gone]
            page += 1
        return collected[offset:offset + limit], len(collected), more or len(collected) > offset + limit

    def _slackmojis_page(self, q: str, page: int) -> tuple[list[LibraryEmoji], bool]:
        key, now = (q.lower(), page), time.time()
        with self._lock:
            hit = self._searches.get(key)
            if hit and now - hit[0] < 900:
                return hit[1], hit[2]
        if self._demo:
            terms = q.lower().split()
            matches = [e for e in DEMO_ITEMS["slackmojis"] if all(t in e.search for t in terms)]
            items = matches[page * SLACKMOJIS_PAGE_SIZE:(page + 1) * SLACKMOJIS_PAGE_SIZE]
            more = len(matches) > (page + 1) * SLACKMOJIS_PAGE_SIZE
        else:
            try:
                r = self._http.get(SLACKMOJIS_SEARCH, params={"query": q, "page": page}, timeout=(5, 20))
                r.raise_for_status()
                items = _parse_slackmojis(r.json())
            except (requests.RequestException, ValueError) as exc:
                log.warning("emoji library: slackmojis search failed: %s", _describe(exc))
                raise EmojiLibraryError(f"slackmojis didn't answer: {_describe(exc)}") from None
            more = len(items) >= SLACKMOJIS_PAGE_SIZE
        items = self._resolve_animated(items)
        with self._lock:
            self._searches[key] = (now, items, more)
            while len(self._searches) > 300:
                self._searches.popitem(last=False)
            for e in items:
                self._seen[e.id] = e
                self._seen.move_to_end(e.id)
            while len(self._seen) > 5000:
                self._seen.popitem(last=False)
        return items, more

    def _resolve_animated(self, items: list[LibraryEmoji]) -> list[LibraryEmoji]:
        """slackmojis GIFs are often a single frame: look at the image (it's cached for the preview)."""
        unknown = [e for e in items if e.animated is None]
        if not unknown:
            return items

        def check(e: LibraryEmoji) -> tuple[str, bool | None]:
            try:
                return e.id, is_animated(self.preview_bytes(e)[0])
            except Exception:  # noqa: BLE001 - an unreadable preview just stays "not known"
                return e.id, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            found = dict(pool.map(check, unknown))
        return [replace(e, animated=found[e.id]) if e.id in found else e for e in items]

    def item(self, source: str, item_id: str) -> LibraryEmoji:
        if source == "slackmojis":
            with self._lock:
                found = self._seen.get(item_id)
        elif source in self._indexes:
            found = self._indexes[source].by_id.get(item_id)
        else:
            found = None
        if found is None:
            raise EmojiLibraryError("that emoji isn't in the library (search for it again)")
        return found

    # ---- images --------------------------------------------------------------------------------

    def preview_bytes(self, item: LibraryEmoji) -> tuple[bytes, str]:
        return self._image(item, item.preview)

    def image_bytes(self, item: LibraryEmoji) -> bytes:
        return self._image(item, item.image)[0]

    def _image(self, item: LibraryEmoji, url: str) -> tuple[bytes, str]:
        if self._demo:
            data = _demo_image(item, url == item.preview)
            return data, sniff(data) or "image/png"
        path = self._previews / hashlib.sha256(url.encode()).hexdigest()
        try:
            data = path.read_bytes()
            kind = sniff(data)
            if kind:
                os.utime(path)  # recently used: pruned last
                return data, kind
        except OSError:
            pass
        host = urlsplit(url)
        if host.scheme != "https" or host.hostname not in IMAGE_HOSTS[item.source]:
            log.warning("emoji library: refused image url host=%s source=%s", host.hostname, item.source)
            raise EmojiLibraryError("that image isn't on the source's image host")
        if url in self._gone():
            raise EmojiLibraryError(f"{LABELS[item.source]} no longer has this image")
        try:
            with self._slots[item.source]:
                data = self._get(url, MAX_IMAGE_BYTES)
        except _HttpError as exc:
            if exc.status in MISSING_STATUSES:
                with self._lock:
                    self._missing[url] = time.time()
                log.info("emoji library: image gone from source (HTTP %s), hidden for a day source=%s id=%s",
                         exc.status, item.source, item.id)
                raise EmojiLibraryError(f"{LABELS[item.source]} no longer has this image") from None
            log.warning("emoji library: image fetch failed source=%s id=%s: %s", item.source, item.id, exc)
            raise EmojiLibraryError(f"{LABELS[item.source]} didn't send the image: {exc}") from None
        except (requests.RequestException, EmojiLibraryError) as exc:
            log.warning("emoji library: image fetch failed source=%s id=%s: %s", item.source, item.id, _describe(exc))
            raise EmojiLibraryError(f"{LABELS[item.source]} didn't send the image: {_describe(exc)}") from None
        kind = sniff(data)
        if kind is None:
            log.warning("emoji library: not an image source=%s id=%s bytes=%d", item.source, item.id, len(data))
            raise EmojiLibraryError("the source sent something that isn't a PNG, GIF, JPEG or WebP image")
        try:
            _write_atomic(path, data)
            self._account(len(data))
        except OSError as exc:  # the cache is a convenience: serve the image anyway
            log.warning("emoji library: preview not cached (%s): %s", type(exc).__name__, exc)
        return data, kind

    def _gone(self) -> set[str]:
        now = time.time()
        with self._lock:
            for url in [u for u, t in self._missing.items() if now - t > MISSING_SECONDS]:
                del self._missing[url]
            return set(self._missing)

    def _cached_files(self) -> list[tuple[Path, int, float]]:
        """(path, size, mtime) of cached previews; skips downloads still being written and files
        another request removed meanwhile."""
        out = []
        for p in self._previews.glob("*"):
            if p.suffix == ".tmp":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p, st.st_size, st.st_mtime))
        return out

    def _account(self, added: int) -> None:
        with self._lock:
            if self._cache_bytes is None:
                self._cache_bytes = sum(size for _, size, _ in self._cached_files())
            else:
                self._cache_bytes += added
            if self._cache_bytes <= PREVIEW_CACHE_BYTES:
                return
            files = sorted(self._cached_files(), key=lambda f: f[2])
            total = sum(size for _, size, _ in files)
            removed = 0
            for p, size, _ in files:
                if total <= PREVIEW_CACHE_BYTES * 0.8:
                    break
                p.unlink(missing_ok=True)
                total -= size
                removed += 1
            self._cache_bytes = total
            log.info("emoji library: preview cache pruned files=%d now=%d bytes", removed, total)

    def _get(self, url: str, limit: int) -> bytes:
        """GET without redirects, refusing bodies over limit."""
        with self._http.get(url, timeout=(5, 30), stream=True, allow_redirects=False) as r:
            if r.status_code != 200:
                raise _HttpError(r.status_code)
            if int(r.headers.get("Content-Length") or 0) > limit:
                raise EmojiLibraryError("response too large")
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                buf += chunk
                if len(buf) > limit:
                    raise EmojiLibraryError("response too large")
            return bytes(buf)

    # ---- demo ----------------------------------------------------------------------------------

    def _load_demo(self) -> None:
        now = time.time()
        for source in self._indexes:
            self._set(source, list(DEMO_ITEMS[source]), updated_at=now, checked_at=now,
                      version="17.0.0" if source == "openmoji" else None)


def _describe(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't connect"
    return str(exc)[:200] or type(exc).__name__


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per thread: two requests for the same preview can download it at once.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def as_dict(e: LibraryEmoji) -> dict[str, Any]:
    d = asdict(e)
    d.pop("search")
    return d


async def run_refresher(library: EmojiLibrary) -> None:
    """At startup and then hourly: download whichever indexes are a day old (or failed an hour ago)."""
    import asyncio

    startup = True
    while True:
        try:
            await asyncio.to_thread(library.refresh, startup)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("emoji library refresh failed")
        startup = False
        await asyncio.sleep(3600)


# Demo mode (WOOSARR_DEMO): a few made-up emoji per source and drawn images, no network.
_DEMO = {
    "emojigg": [("party_parrot", "Animated", True), ("blob_wave", "Blobs", False), ("pepe_popcorn", "Pepe", False),
                ("cat_vibe", "Animated", True), ("thinking_hard", "Thinking", False), ("heart_purple", "Hearts", False),
                ("movie_time", "TV / Movie", False), ("pixel_sword", "Pixel Art", False)],
    "openmoji": [("popcorn", "Food Drink", False), ("film_frames", "Objects", False), ("television", "Objects", False),
                 ("clapper_board", "Objects", False), ("red_heart", "Smileys Emotion", False),
                 ("party_popper", "Activities", False), ("ghost", "Smileys Emotion", False)],
    "slackmojis": [("party_blob", "Party Parrot", True), ("this_is_fine", "Memes", False), ("nyan_cat", "Random", True),
                   ("thumbsup_all", "Random", False), ("cat_typing", "Random", True), ("shipit", "Random", False)],
}
_COLORS = [(155, 89, 182), (52, 152, 219), (230, 126, 34), (46, 204, 113), (231, 76, 60), (241, 196, 15)]
DEMO_ITEMS: dict[str, list[LibraryEmoji]] = {
    source: [LibraryEmoji(source=source, id=str(1000 + n) if source != "openmoji" else f"1F{300 + n:03X}",
                          name=name, title=name, category=cat, animated=anim,
                          image=f"demo://{source}/{name}", preview=f"demo://{source}/{name}/preview",
                          page=f"https://example.invalid/{source}/{name}", credit="demo",
                          rank=100 - n, search=_haystack(name, cat))
             for n, (name, cat, anim) in enumerate(rows)]
    for source, rows in _DEMO.items()
}


def _demo_image(item: LibraryEmoji, preview: bool) -> bytes:
    size = 72 if preview else 128
    color = _COLORS[int(hashlib.sha256(item.name.encode()).hexdigest(), 16) % len(_COLORS)]
    frames = []
    for i in range(4 if item.animated else 1):
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        pad = 4 + i * 3
        d.ellipse((pad, pad, size - pad, size - pad), fill=color + (255,))
        d.text((size // 2, size // 2), item.name[:2].upper(), fill=(255, 255, 255, 255), anchor="mm",
               font_size=size // 3)
        frames.append(im)
    buf = io.BytesIO()
    if len(frames) > 1:
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=150, loop=0, disposal=2)
    else:
        frames[0].save(buf, "PNG")
    return buf.getvalue()
