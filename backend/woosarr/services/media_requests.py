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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from woosarr.core import app_settings, auth, kv
from woosarr.core import secret_store as names
from woosarr.db.models import DiscordLink, DiscordMember, MediaRequest, PlexUser, utcnow
from woosarr.db.session import Database
from woosarr.services import audit, self_resolve
from woosarr.services.discord_text import safe
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
# Failed and declined requests are still checked (and listed) this long after they got that status,
# in case an admin retries or approves them in Seerr.
RECHECK_STATES = ("failed", "declined")
RECHECK_DAYS = 30
# Other finished requests (available, removed in Seerr) leave the Requests page this long after they
# finished; open ones stay, and are checked, however old they are.
CLOSED_VISIBLE_DAYS = 7
# Automatic retries of a failed request (request_auto_retry): the n-th one this long after the
# request failed or was last retried.
RETRY_AFTER = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
# Seerr searches (suggestions + /request text) per member: each one is a TMDB call.
SEARCHES_PER_WINDOW, SEARCH_WINDOW_SECONDS = 30, 60
POLL_SECONDS = 1800

STATE_TEXT = {
    "pending": "waiting for approval",
    "approved": "approved, on its way",
    "declined": "declined",
    "failed": "approved, but downloading failed (an admin can retry it)",
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
    row_id: int | None = None


@dataclass(frozen=True)
class Notice:
    """A status change to tell a member about: as a reply to the public post, or by DM."""

    discord_user_id: str
    text: str
    state: str
    channel_id: str | None = None
    message_id: str | None = None


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
        self.search_limiter = auth.RateLimiter(SEARCHES_PER_WINDOW, SEARCH_WINDOW_SECONDS)

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
        self_resolve.recovered("seerr")

    def _failed(self, exc: SeerrError) -> None:
        if exc.status is None or exc.status in (401,) or exc.status >= 500:
            self.last_error = str(exc)

    # ---- who may request --------------------------------------------------------------

    def may_request(self, session: Session, discord_user_id: str, is_admin: bool) -> PlexUser:
        """WooSarr's own checks (no Seerr calls): roles, link, verified. The linked Plex user, or RequestDenied."""
        settings = app_settings.load(session)
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
        return plex

    def requester(self, session: Session, discord_user_id: str, is_admin: bool) -> tuple[SeerrClient, Requester]:
        """The Seerr client and the member's Seerr user, or RequestDenied."""
        client = self.client()
        if client is None:
            raise RequestDenied("Requests aren't set up yet. Ask an admin.")
        plex = self.may_request(session, discord_user_id, is_admin)
        settings = app_settings.load(session)
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
            raise RequestDenied(f"Seerr isn't answering right now ({safe(str(exc), 200)}). Try again later.") from None
        self._ok()
        if found is None:
            log.warning("no seerr user for plex %s (%s); import %s", plex.username, plex.plex_user_id,
                        "on" if import_missing else "off")
        return found

    # ---- search / details -------------------------------------------------------------

    def search(self, query: str, timeout: float | None = None, member: str | None = None) -> list[Hit]:
        """Seerr search (cached a minute). member: count it against that member's search limit."""
        query = " ".join(query.split())[:100]
        if len(query) < 2:
            return []
        key = query.lower()
        with self._lock:
            cached = self._search.get(key)
        if cached and time.monotonic() - cached[0] < SEARCH_CACHE_SECONDS:
            return cached[1]
        if member is not None and not self.search_limiter.hit(member):
            log.warning("request searches by discord %s over the limit (%s per %s s)", member, SEARCHES_PER_WINDOW,
                        SEARCH_WINDOW_SECONDS)
            raise SeerrError("Too many searches; wait a minute and try again.", 429)
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
            title = f"**{safe(item.title, 200)}**" if item else "That title"
            reason = safe(str(exc), 200)  # Seerr's message
            message = {403: f"Seerr refused: {reason}", 409: f"{title} is already requested."}.get(
                exc.status or 0, f"Seerr couldn't take the request: {reason}")
            with self._db.session() as s:
                audit.record(s, actor, "request_failed", "discord_user", discord_user_id, media_type=media_type,
                             tmdb_id=tmdb_id, seerr_user_id=who.seerr_user_id, status=exc.status, error=str(exc)[:200])
            return Outcome(False, message)
        self._ok()
        if code == 202 or not req or not req.get("id"):
            with self._db.session() as s:
                audit.record(s, actor, "request_failed", "discord_user", discord_user_id, media_type=media_type,
                             tmdb_id=tmdb_id, seerr_user_id=who.seerr_user_id, status=code, error="nothing to request")
            return Outcome(False, f"Every season of **{safe(item.title, 200)}** is already requested or available.")
        state = request_state(req)
        with self._db.session() as s:
            row = MediaRequest(seerr_request_id=int(req["id"]), discord_user_id=discord_user_id,
                               plex_user_id=who.plex_user_id, seerr_user_id=who.seerr_user_id,
                               media_type=media_type, tmdb_id=int(tmdb_id), title=item.title[:300], year=item.year,
                               seasons=sorted(seasons or []), status=state, notified_status=state,
                               checked_at=utcnow(), status_at=utcnow())
            s.add(row)
            s.flush()
            row_id = row.id
            audit.record(s, actor, "request_created", "media_request", str(req["id"]), media_type=media_type,
                         tmdb_id=tmdb_id, title=item.title, seasons=sorted(seasons or []), state=state,
                         seerr_user_id=who.seerr_user_id, plex_user=who.plex_username)
        log.info("request %s created for discord %s as seerr user %s: %s %s (%s)", req["id"], discord_user_id,
                 who.seerr_user_id, media_type, item.title, state)
        what = f"**{safe(item.title, 200)}**" + (f" ({item.year})" if item.year else "")
        return Outcome(True, f"Requested {what}: {STATE_TEXT.get(state, state)}.", state, row_id)

    def retry(self, row_id: int, actor: str) -> str:
        """An admin's retry of a failed request: Seerr sends it to Radarr/Sonarr again. The new status,
        or SeerrError / RequestDenied (not failed anymore, unknown request)."""
        client = self.client()
        if client is None:
            raise RequestDenied("Seerr isn't connected.")
        with self._db.session() as s:
            row = s.get(MediaRequest, row_id)
            if row is None:
                raise RequestDenied("No such request.")
            if row.status != "failed":
                raise RequestDenied("Only failed requests can be retried.")
        return self._retry(client, row_id, actor, automatic=False)

    def _retry(self, client: SeerrClient, row_id: int, actor: str, automatic: bool) -> str:
        with self._db.session() as s:
            request_id = s.get(MediaRequest, row_id).seerr_request_id
        error: SeerrError | None = None
        try:
            state = request_state(client.retry_request(request_id))
        except SeerrError as exc:
            if exc.status == 409:  # not failed in Seerr anymore (retried or approved there): read it
                state = request_state(client.get_request(request_id))
            else:
                self._failed(exc)
                error, state = exc, "failed"
        if error is None:
            self._ok()
        with self._db.session() as s:
            row = s.get(MediaRequest, row_id)
            now = utcnow()
            if automatic:
                row.retries = (row.retries or 0) + 1
            row.retried_at = now
            before = row.status
            if state != before:
                row.status, row.status_at = state, now
            audit.record(s, actor, "request_retried", "media_request", str(request_id), title=row.title,
                         before=before, after=state, automatic=automatic,
                         attempt=row.retries if automatic else None,
                         error=str(error)[:200] if error else None)
            title, attempt = row.title, row.retries
        if error is not None:
            log.warning("request %s (%s): retry in Seerr by %s failed: %s", request_id, title, actor, error)
            if not automatic:
                raise error
        else:
            log.info("request %s (%s) retried in Seerr by %s%s: %s", request_id, title, actor,
                     f" (automatic, try {attempt} of {len(RETRY_AFTER)})" if automatic else "", state)
        return state

    def set_post(self, row_id: int, channel_id: str, message_id: str) -> None:
        """Remember where a public request was posted (status updates reply to it)."""
        with self._db.session() as s:
            row = s.get(MediaRequest, row_id)
            if row is not None:
                row.channel_id, row.message_id = channel_id, message_id

    # ---- status updates ---------------------------------------------------------------

    def poll(self) -> list[Notice]:
        """Re-read open requests (and recently failed or declined ones) from Seerr, and retry failed ones
        that are due. Returns the status changes to tell members about."""
        client = self.client()
        if client is None:
            return []
        with self._db.session() as s:
            settings = app_settings.load(s)
            notify, auto_retry = settings.request_notify, settings.request_auto_retry
            rows = [(r.id, r.seerr_request_id) for r in s.scalars(select(MediaRequest).where(checked()))]
        out: list[Notice] = []
        changed = 0
        due: list[int] = []
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
                now = row.checked_at = utcnow()
                was = row.status
                if state != was:
                    changed += 1
                    audit.record(s, "system", "request_status", "media_request", str(request_id),
                                 before=was, after=state, title=row.title)
                    row.status, row.status_at = state, now
                # A failed request WooSarr will retry: the member hears of it only if every try fails.
                retrying = state == "failed" and auto_retry and (row.retries or 0) < len(RETRY_AFTER)
                if retrying and retry_due(row, now):
                    due.append(row_id)
                held = state == "failed" and was == "failed"  # failed while retrying, now out of tries
                if (notify and state in NOTIFY_STATES and row.notified_status != state and not retrying
                        and (state != was or held)):
                    row.notified_status = state
                    what = f"**{safe(row.title, 200)}**" + (f" ({row.year})" if row.year else "")
                    out.append(Notice(row.discord_user_id, f"Your request for {what} is {STATE_TEXT[state]}.", state,
                                      row.channel_id, row.message_id))
        else:
            for row_id in due:
                self._retry(client, row_id, "system", automatic=True)
        if rows:
            log.info("request poll: %s checked, %s changed, %s to notify, %s retried", len(rows), changed, len(out),
                     len(due))
        return out


def checked():
    """The requests poll() re-reads: open ones, and failed or declined ones for RECHECK_DAYS."""
    return or_(MediaRequest.status.in_(OPEN_STATES),
               MediaRequest.status.in_(RECHECK_STATES) & (MediaRequest.status_at >= utcnow() - timedelta(days=RECHECK_DAYS)))


def next_retry_at(row: MediaRequest, auto_retry: bool = True) -> Any:
    """When a failed request is retried next: RETRY_AFTER after it failed or was last retried."""
    tries = row.retries or 0
    if not auto_retry or row.status != "failed" or tries >= len(RETRY_AFTER):
        return None
    since = max(t for t in (row.status_at or row.created_at, row.retried_at) if t is not None)
    return since + RETRY_AFTER[tries]


def retry_due(row: MediaRequest, now: Any) -> bool:
    when = next_retry_at(row)
    return when is not None and now >= when


def recent(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    """The Requests page: every request still being checked (open, recently failed or declined), plus
    finished ones from the last week (newest first; limit caps the finished ones)."""
    newest = MediaRequest.created_at.desc()
    open_rows = list(session.scalars(select(MediaRequest).where(checked()).order_by(newest)))
    closed = session.scalars(select(MediaRequest).where(
        MediaRequest.id.not_in([r.id for r in open_rows]),
        MediaRequest.status_at >= utcnow() - timedelta(days=CLOSED_VISIBLE_DAYS)).order_by(newest).limit(limit))
    rows = sorted([*open_rows, *closed], key=lambda r: r.created_at, reverse=True)
    return rows_out(session, rows)


def rows_out(session: Session, rows: list[MediaRequest]) -> list[dict[str, Any]]:
    members = {m.discord_user_id: m for m in session.scalars(select(DiscordMember).where(
        DiscordMember.discord_user_id.in_({r.discord_user_id for r in rows})))}
    plex = dict(session.execute(select(PlexUser.plex_user_id, PlexUser.username).where(
        PlexUser.plex_user_id.in_({r.plex_user_id for r in rows if r.plex_user_id}))).all())
    auto_retry = app_settings.load(session).request_auto_retry
    return [{"id": r.id, "seerr_request_id": r.seerr_request_id, "media_type": r.media_type, "tmdb_id": r.tmdb_id,
             "title": r.title, "year": r.year, "seasons": list(r.seasons or []), "status": r.status,
             "discord_user_id": r.discord_user_id,
             "discord_name": (members[r.discord_user_id].display_name or members[r.discord_user_id].username)
             if r.discord_user_id in members else None,
             "plex_username": plex.get(r.plex_user_id or ""), "public": r.message_id is not None,
             "created_at": r.created_at, "updated_at": r.updated_at,
             "checked_at": r.checked_at, "status_at": r.status_at, "retries": r.retries or 0,
             "retried_at": r.retried_at, "next_retry_at": next_retry_at(r, auto_retry)} for r in rows]


async def run_poller(service: RequestService, send: Any, interval: float = POLL_SECONDS,
                     first: float = 60) -> None:
    """Forever: poll open requests and tell members about changes (send = async (Notice) -> bool)."""
    await asyncio.sleep(first)
    while True:
        try:
            for notice in await asyncio.to_thread(service.poll):
                await send(notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("request poll failed")
        await asyncio.sleep(interval)
