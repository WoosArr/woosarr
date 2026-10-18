"""Recent warnings/errors grouped for the dashboard, with "mark resolved".

Log entries come from the in-memory buffer (last 24 h). Repeats of the same
problem are grouped by a fingerprint: level + logger + the message with
numbers, ids and quoted values normalised. Resolving a group hides its
occurrences up to 24 h after the resolve time from the dashboard (the Logs
page still shows everything); if it happens again after that, it's back.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from woosarr.core import logs as logcore
from woosarr.db.models import ErrorResolution, utcnow

WINDOW = timedelta(hours=24)
RESOLVE_FOR = timedelta(hours=24)

_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUM = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']{1,200}'|\"[^\"]{1,200}\"")
_SPACE = re.compile(r"\s+")


def normalize(message: str) -> str:
    text = _QUOTED.sub("'…'", message)
    text = _HEX.sub("#", text)
    text = _NUM.sub("N", text)
    return _SPACE.sub(" ", text).strip()[:200]


def fingerprint(level: str, logger: str, message: str) -> str:
    level = "ERROR" if level == "CRITICAL" else level
    return hashlib.sha256(f"{level}|{logger}|{normalize(message)}".encode()).hexdigest()[:16]


def _exc_summary(exc: str | None) -> str | None:
    if not exc:
        return None
    lines = [line for line in exc.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else None


@dataclass
class Group:
    fingerprint: str
    level: str
    logger: str
    message: str  # latest occurrence
    count: int = 0  # shown on the dashboard
    hidden: int = 0  # covered by a resolution
    first_ts: str = ""
    last_ts: str = ""
    exc: str | None = None
    resolved_until: datetime | None = None
    resolved_by: str | None = None


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def groups(session: Session, min_level: int = logging.WARNING) -> list[Group]:
    """Groups seen in the last 24 h, most recent first (resolved ones included, count 0)."""
    now = utcnow()
    session.execute(delete(ErrorResolution).where(ErrorResolution.until < now - WINDOW))
    resolutions = {r.fingerprint: r for r in session.scalars(select(ErrorResolution))}
    entries, _ = logcore.buffer().query(min_level=min_level, limit=100_000)
    cutoff = now - WINDOW
    out: dict[str, Group] = {}
    for e in entries:
        when = _ts(e.ts)
        if when < cutoff:
            continue
        fp = fingerprint(e.level, e.logger, e.message)
        g = out.get(fp)
        if g is None:
            g = out[fp] = Group(fp, "ERROR" if e.level == "CRITICAL" else e.level, e.logger, e.message,
                                first_ts=e.ts)
        g.message, g.last_ts = e.message, e.ts
        g.exc = _exc_summary(e.exc) or g.exc
        res = resolutions.get(fp)
        if res is not None and when <= res.until:
            g.hidden += 1
        else:
            g.count += 1
    for fp, g in out.items():
        res = resolutions.get(fp)
        if res is not None and res.until > now:
            g.resolved_until, g.resolved_by = res.until, res.resolved_by
    return sorted(out.values(), key=lambda g: g.last_ts, reverse=True)


def visible_counts(session: Session) -> tuple[int, int]:
    """(warnings, errors) in the last 24 h that aren't covered by a resolution."""
    warnings = errors = 0
    for g in groups(session):
        if g.level == "WARNING":
            warnings += g.count
        else:
            errors += g.count
    return warnings, errors


def resolve(session: Session, group: Group, actor: str) -> ErrorResolution:
    now = utcnow()
    row = session.get(ErrorResolution, group.fingerprint)
    if row is None:
        row = ErrorResolution(fingerprint=group.fingerprint)
        session.add(row)
    row.level, row.logger, row.message = group.level, group.logger[:100], group.message[:300]
    row.resolved_at, row.resolved_by, row.until = now, actor, now + RESOLVE_FOR
    return row


def unresolve(session: Session, fp: str) -> bool:
    row = session.get(ErrorResolution, fp)
    if row is None:
        return False
    session.delete(row)
    return True
