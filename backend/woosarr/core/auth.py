"""Local admin authentication (plan D7, P6-01, P6-06, P10-07).

* Passwords: argon2id (argon2-cffi defaults, RFC 9106 low-memory profile).
* Sessions: random 256-bit cookie value; only its sha256 is stored. Idle and
  absolute expiry. A new session id on every login (no fixation).
* CSRF: per-session token that the SPA sends as X-CSRF-Token on unsafe methods.
* API keys: `wsk_<prefix>_<secret>`, stored as sha256, scoped read/admin.
* Brute force: per-account lockout plus a per-IP sliding-window limiter.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from woosarr.db.models import AdminUser, ApiKey, utcnow
from woosarr.db.models import Session as DbSession

SESSION_COOKIE = "woosarr_session"
CSRF_HEADER = "X-CSRF-Token"
API_KEY_PREFIX = "wsk_"
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so timing doesn't reveal it.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AuthError(Exception):
    """Login failed. The message is safe to show (never says which part was wrong)."""


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    check_password_policy(password)
    return _hasher.hash(password)


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > 1024:
        raise ValueError("password is too long")


def _verify(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def active_admin_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(AdminUser).where(AdminUser.is_active.is_(True))
    ) or 0


def admin_exists(session: Session) -> bool:
    return (session.scalar(select(func.count()).select_from(AdminUser)) or 0) > 0


def authenticate(session: Session, username: str, password: str) -> AdminUser:
    user = session.scalar(select(AdminUser).where(AdminUser.username == username))
    now = utcnow()
    if user is None:
        _verify(None, password)
        raise AuthError("invalid username or password")
    if user.locked_until and user.locked_until > now:
        _verify(None, password)
        raise AuthError("account temporarily locked after too many failed logins")
    if not user.is_active or user.auth_source != "local" or not _verify(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT
            user.failed_logins = 0
        raise AuthError("invalid username or password")
    if _hasher.check_needs_rehash(user.password_hash or ""):
        user.password_hash = _hasher.hash(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    return user


@dataclass(frozen=True)
class NewSession:
    cookie_value: str
    csrf_token: str
    expires_at: datetime


def create_session(
    session: Session, user: AdminUser, max_age: timedelta, ip: str | None, user_agent: str | None
) -> NewSession:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = utcnow() + max_age
    session.add(
        DbSession(
            id_hash=sha256(raw),
            admin_user_id=user.id,
            csrf_token=csrf,
            expires_at=expires,
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    return NewSession(raw, csrf, expires)


def resolve_session(
    session: Session, cookie_value: str, idle: timedelta
) -> tuple[DbSession, AdminUser] | None:
    row = session.get(DbSession, sha256(cookie_value))
    if row is None:
        return None
    now = utcnow()
    if row.expires_at <= now or row.last_seen_at + idle <= now:
        session.delete(row)
        return None
    user = session.get(AdminUser, row.admin_user_id)
    if user is None or not user.is_active:
        session.delete(row)
        return None
    # Write at most once a minute to keep reads cheap.
    if now - row.last_seen_at > timedelta(minutes=1):
        row.last_seen_at = now
    return row, user


def end_session(session: Session, cookie_value: str) -> None:
    session.execute(delete(DbSession).where(DbSession.id_hash == sha256(cookie_value)))


def end_all_sessions(session: Session, admin_user_id: int, keep_hash: str | None = None) -> None:
    stmt = delete(DbSession).where(DbSession.admin_user_id == admin_user_id)
    if keep_hash:
        stmt = stmt.where(DbSession.id_hash != keep_hash)
    session.execute(stmt)


def prune_sessions(session: Session) -> int:
    return session.execute(delete(DbSession).where(DbSession.expires_at <= utcnow())).rowcount or 0


def csrf_ok(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(expected, supplied or "")


# ---- API keys -------------------------------------------------------------


@dataclass(frozen=True)
class NewApiKey:
    row: ApiKey
    raw: str  # shown to the admin exactly once


def create_api_key(session: Session, user: AdminUser, name: str, scope: str) -> NewApiKey:
    if scope not in ("read", "admin"):
        raise ValueError("scope must be 'read' or 'admin'")
    prefix = secrets.token_hex(4)
    raw = f"{API_KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    row = ApiKey(name=name, prefix=prefix, key_hash=sha256(raw), admin_user_id=user.id, scope=scope)
    session.add(row)
    return NewApiKey(row, raw)


def resolve_api_key(session: Session, raw: str) -> tuple[ApiKey, AdminUser] | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or f"{parts[0]}_" != API_KEY_PREFIX:
        return None
    row = session.scalar(select(ApiKey).where(ApiKey.prefix == parts[1]))
    if row is None or row.revoked_at is not None or not hmac.compare_digest(row.key_hash, sha256(raw)):
        return None
    user = session.get(AdminUser, row.admin_user_id)
    if user is None or not user.is_active:
        return None
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now
    return row, user


# ---- Rate limiting ----------------------------------------------------------


class RateLimiter:
    """In-memory sliding window per key (client IP). Single process, so this is enough."""

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def hit(self, key: str) -> bool:
        """Record an attempt; False if the key is over the limit."""
        now = time.monotonic()
        with self._lock:
            hits = self._recent(key, now)
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            return True

    def blocked(self, key: str) -> bool:
        """Over the limit right now (without recording anything)."""
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.max_attempts

    def record(self, key: str) -> None:
        """Count one failed attempt."""
        with self._lock:
            self._recent(key, time.monotonic()).append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class SetupCode:
    """One-time code that gates first-run admin creation (P6-06).

    Shown only on the container's stdout, never through logging (which feeds
    the log file and the logs API).
    """

    def __init__(self) -> None:
        raw = secrets.token_hex(6)  # 48 bits; guessing is also rate limited per IP
        self.value = "-".join(raw[i : i + 4] for i in range(0, 12, 4))

    @staticmethod
    def _normalize(code: str) -> str:
        return code.strip().lower().replace("-", "").replace(" ", "")

    def matches(self, supplied: str | None) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(self._normalize(self.value), self._normalize(supplied))
