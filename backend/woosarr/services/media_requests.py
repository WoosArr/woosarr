"""Movie/TV requests through Seerr for linked Discord members (plan P13-01).

Requests are made *as the member's Seerr user* (X-API-User), so Seerr's own
permissions, quotas and auto-approve rules apply. A member is mapped to a
Seerr user through their WooSarr link: Discord -> Plex user -> Seerr user
with that plexId (falling back to email / Plex username). WooSarr only adds
who may use the command (linked, verified, roles) and tells members when a
request changes status.

Every DB call takes a Session; Seerr calls are synchronous (worker threads).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.db.models import DiscordLink, DiscordMember, MediaRequest, PlexUser, utcnow
from woosarr.db.session import Database
from woosarr.services import audit
from woosarr.services.seerr import (
    Details,
    Hit,
    SeerrClient,
    SeerrError,
    normalize_url,
    request_state,
)

log = logging.getLogger("woosarr.requests")

K_SEERR = "seerr.config"  # {"url", "version", "admin"}
USER_CACHE_SECONDS = 600
SEARCH_CACHE_SECONDS = 60
OPEN_STATES = ("pending", "approved", "partially_available")
NOTIFY_STATES = ("approved", "declined", "failed", "partially_available", "available")
TRACK_DAYS = 60
POLL_SECONDS = 300

STATE_TEXT = {
    "pending": "waiting for approval",
    "approved": "approved, on its way",
    "declined": "declined",
    "failed": "approved, but downloading failed (an admin can retry it in Seerr)",
    "partially_available": "partly available on Plex",
    "available": "available on Plex",
    "deleted": "removed from Seerr",
}


@dataclass(frozen=True)
class Requester:
    discord_user_id: str
    plex_user_id: str
    plex_username: str
    seerr_user_id: int


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    state: str | None = None


class RequestDenied(Exception):
    """The member can't request; message is safe to show them."""


class RequestService:
    def __init__(self, db: Database, secrets: Any, session_factory: Any = None) -> None:
        self._db = db
        self._secrets = secrets
        self._session_factory = session_factory  # tests inject a fake HTTP session
        self._lock = threading.Lock()
        self._users: tuple[float, list[dict]] | None = None
        self._search: dict[str, tuple[float, list[Hit]]] = {}
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    # ---- configuration ----------------------------------------------------------------

    def _make(self, url: str, key: str) -> SeerrClient:
        session = self._session_factory() if self._session_factory else None
        return SeerrClient(url, key, session=session)

    def client(self) -> SeerrClient | None:
        with self._db.session() as s:
            cfg = kv.get(s, K_SEERR)
            try:
                key = self._secrets.get(s, names.SEERR_API_KEY)
            except Exception:
                log.exception("stored Seerr API key can't be read; enter it again")
                return None
        if not cfg or not key:
            return None
        return self._make(cfg["url"], key)

    def configured(self) -> bool:
        with self._db.session() as s:
            return bool(kv.get(s, K_SEERR)) and self._secrets.is_set(s, names.SEERR_API_KEY)

    def test(self, url: str, key: str | None) -> dict[str, Any]:
        """Check address + key without saving. key=None reuses the stored key."""
        url = normalize_url(url)
        if key is None:
            with self._db.session() as s:
                key = self._secrets.get(s, names.SEERR_API_KEY)
            if not key:
                raise SeerrError("Enter the Seerr API key.")
        client = self._make(url, key.strip())
        status = client.server_status()
        me = client.me()
        users = client.users()
        result = {"url": url, "version": str(status.get("version", "")),
                  "admin": str(me.get("displayName") or me.get("username") or me.get("plexUsername") or me["id"]),
                  "users": len(users), "plex_users": sum(1 for u in users if u.get("plexId"))}
        log.info("seerr test ok url=%s version=%s users=%s (plex %s)", url, result["version"], result["users"],
                 result["plex_users"])
        return result

    def save(self, url: str, key: str | None) -> dict[str, Any]:
        info = self.test(url, key)
        with self._db.session() as s:
            if key:
                self._secrets.set(s, names.SEERR_API_KEY, key.strip())
            kv.put(s, K_SEERR, {"url": info["url"], "version": info["version"], "admin": info["admin"]})
        self._forget()
        self.last_error, self.last_ok_at = None, time.time()
        return info

    def disconnect(self) -> None:
        with self._db.session() as s:
            self._secrets.delete(s, names.SEERR_API_KEY)
            kv.delete(s, K_SEERR)
        self._forget()
        self.last_error = None
        log.info("seerr disconnected and API key deleted")

    def _forget(self) -> None:
        with self._lock:
            self._users, self._search = None, {}

    def status(self) -> dict[str, Any]:
        with self._db.session() as s:
            cfg = kv.get(s, K_SEERR)
            has_key = self._secrets.is_set(s, names.SEERR_API_KEY)
        configured = bool(cfg and has_key)
        return {"configured": configured, "connected": configured and self.last_error is None,
                "url": cfg["url"] if cfg else None, "version": cfg.get("version") if cfg else None,
                "admin": cfg.get("admin") if cfg else None,
                "detail": None if configured and not self.last_error else
                (self.last_error if configured else "Not connected"),
                "last_ok_at": self.last_ok_at}

    def _ok(self) -> None:
        self.last_error, self.last_ok_at = None, time.time()

    def _failed(self, exc: SeerrError) -> None:
        if exc.status is None or exc.status in (401,) or exc.status >= 500:
            self.last_error = str(exc)

    # ---- who may request --------------------------------------------------------------

    def requester(self, session: Session, discord_user_id: str, is_admin: bool) -> tuple[SeerrClient, Requester]:
        """The Seerr client and the member's Seerr user, or RequestDenied."""
        settings = app_settings.load(session)
        client = self.client()
        if client is None:
            raise RequestDenied("Requests aren't set up yet. Ask an admin.")
        if not is_admin and not settings.request_all_members:
            member = session.get(DiscordMember, discord_user_id)
            allowed = set(settings.request_role_ids)
            if not allowed or member is None or not member.in_guild or not allowed & set(member.role_ids):
                raise RequestDenied("Your Discord roles don't allow requests.")
        link = session.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id))
        if link is None:
            raise RequestDenied("Link your Plex account first with `/link`, so requests are made as you.")
        if settings.request_require_verified and not link.verified:
            raise RequestDenied("Requests need a verified link: run `/link` and choose **Sign in with Plex**.")
        plex = session.get(PlexUser, link.plex_user_id)
        if plex is None or plex.removed:
            raise RequestDenied("Your linked Plex account isn't on this server anymore. Ask an admin.")
        seerr_id = self._seerr_user(client, plex, settings.request_import_users)
        if seerr_id is None:
            raise RequestDenied("You don't have a Seerr account yet. Ask an admin to import you in Seerr "
                                "(Users → Import Plex Users).")
        return client, Requester(discord_user_id, plex.plex_user_id, plex.username, seerr_id)

    def _seerr_users(self, client: SeerrClient, fresh: bool = False) -> list[dict]:
        with self._lock:
            cached = self._users
        if cached and not fresh and time.monotonic() - cached[0] < USER_CACHE_SECONDS:
            return cached[1]
        users = client.users()
        with self._lock:
            self._users = (time.monotonic(), users)
        return users

    @staticmethod
    def _match(users: list[dict], plex: PlexUser) -> int | None:
        if plex.plex_user_id.isdigit():
            for u in users:
                if str(u.get("plexId") or "") == plex.plex_user_id:
                    return int(u["id"])
        email = (plex.email or "").lower()
        name = (plex.username or "").lower()
        for u in users:
            if email and str(u.get("email") or "").lower() == email:
                return int(u["id"])
        for u in users:
            if name and str(u.get("plexUsername") or "").lower() == name:
                return int(u["id"])
        return None

    def _seerr_user(self, client: SeerrClient, plex: PlexUser, import_missing: bool) -> int | None:
        try:
            found = self._match(self._seerr_users(client), plex)
            if found is None:
                found = self._match(self._seerr_users(client, fresh=True), plex)
            if found is None and import_missing and plex.plex_user_id.isdigit():
                created = client.import_plex_users([plex.plex_user_id])
                log.info("seerr import of plex user %s (%s): %s created", plex.username, plex.plex_user_id, created)
                found = self._match(self._seerr_users(client, fresh=True), plex)
        except SeerrError as exc:
            self._failed(exc)
            log.warning("seerr user lookup for plex %s failed: %s", plex.plex_user_id, exc)
            raise RequestDenied(f"Seerr isn't answering right now ({exc}). Try again later.") from None
        self._ok()
        if found is None:
            log.warning("no seerr user for plex %s (%s); import %s", plex.username, plex.plex_user_id,
                        "on" if import_missing else "off")
        return found

    # ---- search / details -------------------------------------------------------------

    def search(self, query: str, timeout: float | None = None) -> list[Hit]:
        query = " ".join(query.split())[:100]
        if len(query) < 2:
            return []
        key = query.lower()
        with self._lock:
            cached = self._search.get(key)
        if cached and time.monotonic() - cached[0] < SEARCH_CACHE_SECONDS:
            return cached[1]
        client = self.client()
        if client is None:
            return []
        try:
            hits = client.search(query, timeout=timeout)
        except SeerrError as exc:
            self._failed(exc)
            raise
        self._ok()
        with self._lock:
            if len(self._search) > 200:
                self._search.clear()
            self._search[key] = (time.monotonic(), hits)
        return hits

    def details(self, media_type: str, tmdb_id: int) -> Details:
        client = self.client()
        if client is None:
            raise SeerrError("Requests aren't set up yet.")
        try:
            return client.details(media_type, tmdb_id)
        except SeerrError as exc:
            self._failed(exc)
            raise

    # ---- submit -------------------------------------------------------------------------

    def submit(self, discord_user_id: str, is_admin: bool, media_type: str, tmdb_id: int,
               seasons: list[int] | None = None) -> Outcome:
        actor = f"discord:{discord_user_id}"
        with self._db.session() as s:
            try:
                client, who = self.requester(s, discord_user_id, is_admin)
            except RequestDenied as exc:
                audit.record(s, actor, "request_denied", "discord_user", discord_user_id, reason=str(exc),
                             media_type=media_type, tmdb_id=tmdb_id)
                return Outcome(False, str(exc))
        item: Details | None = None
        try:
            item = client.details(media_type, tmdb_id)
            code, req = client.create_request(who.seerr_user_id, media_type, tmdb_id, seasons)
        except SeerrError as exc:
            self._failed(exc)
            title = f"**{item.title}**" if item else "That title"
            message = {403: f"Seerr refused: {exc}", 409: f"{title} is already requested."}.get(
                exc.status or 0, f"Seerr couldn't take the request: {exc}")
            with self._db.session() as s:
                audit.record(s, actor, "request_failed", "discord_user", discord_user_id, media_type=media_type,
                             tmdb_id=tmdb_id, seerr_user_id=who.seerr_user_id, status=exc.status, error=str(exc)[:200])
            return Outcome(False, message)
        self._ok()
        if code == 202 or not req or not req.get("id"):
            with self._db.session() as s:
                audit.record(s, actor, "request_failed", "discord_user", discord_user_id, media_type=media_type,
                             tmdb_id=tmdb_id, seerr_user_id=who.seerr_user_id, status=code, error="nothing to request")
            return Outcome(False, f"Every season of **{item.title}** is already requested or available.")
        state = request_state(req)
        with self._db.session() as s:
            s.add(MediaRequest(seerr_request_id=int(req["id"]), discord_user_id=discord_user_id,
                               plex_user_id=who.plex_user_id, seerr_user_id=who.seerr_user_id,
                               media_type=media_type, tmdb_id=int(tmdb_id), title=item.title[:300], year=item.year,
                               seasons=sorted(seasons or []), status=state, notified_status=state,
                               checked_at=utcnow()))
            audit.record(s, actor, "request_created", "media_request", str(req["id"]), media_type=media_type,
                         tmdb_id=tmdb_id, title=item.title, seasons=sorted(seasons or []), state=state,
                         seerr_user_id=who.seerr_user_id, plex_user=who.plex_username)
        log.info("request %s created for discord %s as seerr user %s: %s %s (%s)", req["id"], discord_user_id,
                 who.seerr_user_id, media_type, item.title, state)
        what = f"**{item.title}**" + (f" ({item.year})" if item.year else "")
        return Outcome(True, f"Requested {what}: {STATE_TEXT.get(state, state)}.", state)

    # ---- status updates ---------------------------------------------------------------

    def poll(self) -> list[tuple[str, str]]:
        """Re-read open requests from Seerr. Returns (discord_user_id, message) to notify."""
        client = self.client()
        if client is None:
            return []
        with self._db.session() as s:
            notify = app_settings.load(s).request_notify
            rows = [(r.id, r.seerr_request_id) for r in s.scalars(select(MediaRequest).where(
                MediaRequest.status.in_(OPEN_STATES),
                MediaRequest.created_at >= utcnow() - timedelta(days=TRACK_DAYS)))]
        out: list[tuple[str, str]] = []
        changed = 0
        for row_id, request_id in rows:
            try:
                state = request_state(client.get_request(request_id))
            except SeerrError as exc:
                if exc.status == 404:
                    state = "deleted"
                else:
                    self._failed(exc)
                    log.warning("request poll stopped at %s: %s", request_id, exc)
                    break
            self._ok()
            with self._db.session() as s:
                row = s.get(MediaRequest, row_id)
                row.checked_at = utcnow()
                if state == row.status:
                    continue
                changed += 1
                audit.record(s, "system", "request_status", "media_request", str(request_id),
                             before=row.status, after=state, title=row.title)
                row.status = state
                if notify and state in NOTIFY_STATES and row.notified_status != state:
                    row.notified_status = state
                    what = f"**{row.title}**" + (f" ({row.year})" if row.year else "")
                    out.append((row.discord_user_id, f"Your request for {what} is {STATE_TEXT[state]}."))
        if rows:
            log.info("request poll: %s open, %s changed, %s to notify", len(rows), changed, len(out))
        return out


def recent(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    rows = session.scalars(select(MediaRequest).order_by(MediaRequest.created_at.desc()).limit(limit)).all()
    members = {m.discord_user_id: m for m in session.scalars(select(DiscordMember).where(
        DiscordMember.discord_user_id.in_({r.discord_user_id for r in rows})))}
    plex = dict(session.execute(select(PlexUser.plex_user_id, PlexUser.username).where(
        PlexUser.plex_user_id.in_({r.plex_user_id for r in rows if r.plex_user_id}))).all())
    return [{"id": r.id, "seerr_request_id": r.seerr_request_id, "media_type": r.media_type, "tmdb_id": r.tmdb_id,
             "title": r.title, "year": r.year, "seasons": list(r.seasons or []), "status": r.status,
             "discord_user_id": r.discord_user_id,
             "discord_name": (members[r.discord_user_id].display_name or members[r.discord_user_id].username)
             if r.discord_user_id in members else None,
             "plex_username": plex.get(r.plex_user_id or ""), "created_at": r.created_at, "updated_at": r.updated_at,
             "checked_at": r.checked_at} for r in rows]


async def run_poller(service: RequestService, send: Any, interval: float = POLL_SECONDS,
                     first: float = 60) -> None:
    """Forever: poll open requests and DM members about changes (send = async (uid, text))."""
    await asyncio.sleep(first)
    while True:
        try:
            for discord_user_id, text in await asyncio.to_thread(service.poll):
                await send(discord_user_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("request poll failed")
        await asyncio.sleep(interval)
