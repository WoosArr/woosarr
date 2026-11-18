"""A locked or overloaded database (live 2026-11-18: the host's disk stalled, one write held SQLite's lock,
and every request that wanted to write queued behind it until all 15 pooled connections were taken).

Such a request is refused with 503 instead of crashing with 500. The refusal is logged as one warning a
minute at most (the rest at INFO), and that warning resolves itself once a write commits again.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import exc as sa_exc

log = logging.getLogger("woosarr.app")

WARN_EVERY_SECONDS = 60.0
RECOVERY_KEY = "db"

_lock = threading.Lock()
_pending = False  # a refusal was logged and no write has committed since
_last_warning = 0.0
_quiet = 0  # refusals logged at INFO since the last warning


def reason(exc: BaseException) -> str | None:
    """Why the database couldn't serve the request, or None when exc is something else."""
    if isinstance(exc, sa_exc.TimeoutError):
        return "every database connection was in use for 30 s (requests were waiting on a locked database)"
    if isinstance(exc, sa_exc.OperationalError):
        text = str(getattr(exc, "orig", exc)).lower()
        if "database is locked" in text or "database table is locked" in text or "database is busy" in text:
            return "the database stayed locked by another write for 30 s"
    return None


def refused(method: str, path: str, why: str, request_id: str) -> None:
    global _pending, _last_warning, _quiet
    now = time.monotonic()
    with _lock:
        _pending = True
        warn = now - _last_warning >= WARN_EVERY_SECONDS
        if warn:
            _last_warning, quiet, _quiet = now, _quiet, 0
        else:
            _quiet += 1
    if warn:
        more = f"; {quiet} more refused since the last warning" if quiet else ""
        log.warning("database busy: answered %s %s with 503 (request_id=%s): %s. Usually the host's disk was "
                    "stalled; the page retries by itself%s", method, path, request_id, why, more)
    else:
        log.info("database busy: answered %s %s with 503 (request_id=%s): %s", method, path, request_id, why)


def write_committed() -> None:
    """A write reached the database: the busy warning, if any, is over."""
    global _pending
    if not _pending:  # the common case: no lock, no import
        return
    with _lock:
        was, _pending = _pending, False
    if was:
        from woosarr.services import self_resolve  # here, not at the top: services import db

        self_resolve.recovered(RECOVERY_KEY)
        log.info("database writes work again")


def reset() -> None:  # tests
    global _pending, _last_warning, _quiet
    with _lock:
        _pending, _last_warning, _quiet = False, 0.0, 0
