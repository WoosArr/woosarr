"""Dashboard Alerts: grouped warnings/errors plus account-takeover events.

Two sources, no time window:
- Log entries at WARNING and up from the in-memory buffer (refilled from the log
  files at startup). Repeats of the same problem are grouped by a fingerprint:
  level + logger + the message with numbers, ids and quoted values normalised.
- Account alerts from the audit log: someone tried to link a Plex account that is
  linked to another Discord member, took over unverified claims or joined a shared
  (family) account by signing in with Plex, or an admin added a member to an account
  others are linked to. Grouped per kind, Plex account and Discord member.

Resolving a group hides its occurrences up to 24 h after the resolve time from the
dashboard (the Logs page still shows everything); if it happens again after that,
it's back. A resolution is kept as long as anything it hides still exists.
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
from woosarr.db.models import AuditLog, DiscordMember, ErrorResolution, PlexUser, utcnow

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


ACCOUNT_LOGGER = "woosarr.accounts"
_METHOD = {"username": "by typing its username", "oauth": "by signing in with Plex", "invite": "through an invite"}


def _ids(d: dict, many: str, one: str) -> list[str]:
    """A list of Discord ids from audit details (newer rows store a list, older ones a single id)."""
    value = d.get(many)
    if value:
        return [str(x) for x in value]
    return [str(d[one])] if d.get(one) else []


def _account_alerts(session: Session) -> list[tuple[str, str, str]]:
    """(fingerprint key, ISO timestamp, message) for each account-takeover event in the audit log, oldest first."""
    rows = session.scalars(select(AuditLog).where(AuditLog.action.in_(("link_failed", "link_self", "link_admin")))
                           .order_by(AuditLog.ts, AuditLog.id)).all()
    events = []
    for row in rows:
        d = row.details or {}
        taken = d.get("reason") == "plex_account_taken"
        # A member who already has a link tried someone else's Plex account.
        grab = d.get("reason") == "discord_already_linked" and bool(_ids(d, "holder_discord_user_ids",
                                                                         "holder_discord_user_id"))
        if row.action == "link_failed" and (taken or grab):
            events.append((row, "attempt"))
        elif row.action == "link_self" and d.get("outcome") == "replaced":
            events.append((row, "takeover"))
        elif row.action == "link_self" and d.get("outcome") == "joined":
            events.append((row, "joined"))
        elif row.action == "link_admin" and d.get("other_discord_user_ids"):
            events.append((row, "added"))
        elif (row.action == "link_admin" and d.get("previous_discord_user_id")
              and d.get("previous_discord_user_id") != d.get("discord_user_id")):
            events.append((row, "moved"))  # before shared accounts, an admin link replaced the old one
    if not events:
        return []
    discord_ids = set()
    plex_ids = set()
    for row, _kind in events:
        d = row.details or {}
        discord_ids.add(row.actor.removeprefix("discord:"))
        discord_ids.update(_ids(d, "holder_discord_user_ids", "holder_discord_user_id"))
        discord_ids.update(_ids(d, "displaced_discord_user_ids", "displaced_discord_user_id"))
        discord_ids.update(_ids(d, "other_discord_user_ids", "previous_discord_user_id"))
        discord_ids.update(x for x in (d.get("discord_user_id"),) if x)
        plex_ids.update(x for x in (row.target_id, d.get("plex_user_id"), d.get("own_plex_user_id")) if x)
    members = {m.discord_user_id: m for m in session.scalars(
        select(DiscordMember).where(DiscordMember.discord_user_id.in_(discord_ids)))}
    plex = dict(session.execute(select(PlexUser.plex_user_id, PlexUser.username)
                                .where(PlexUser.plex_user_id.in_(plex_ids))).all())

    def who(discord_id: str | None) -> str:
        if not discord_id:
            return "another member"
        m = members.get(discord_id)
        name = (m.display_name or m.username) if m else None
        return f"{name} (Discord {discord_id})" if name else f"Discord member {discord_id}"

    def whos(ids: list[str]) -> str:
        names = [who(x) for x in ids] or [who(None)]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

    def account(plex_id: str | None) -> str:
        return f"Plex account {plex.get(plex_id, plex_id or '?')}"

    def removed(ids: list[str]) -> str:
        if not ids:
            return ""
        return f" The unverified link{'s' if len(ids) > 1 else ''} of {whos(ids)} {'were' if len(ids) > 1 else 'was'} removed."

    out = []
    for row, kind in events:
        d = row.details or {}
        ts = row.ts.isoformat(timespec="milliseconds")
        member = row.actor.removeprefix("discord:")
        if kind == "attempt":
            target = d.get("plex_user_id") or row.target_id
            holders = _ids(d, "holder_discord_user_ids", "holder_discord_user_id")
            held_by = (f"which is linked to {whos(holders)}" + (" (verified)" if d.get("holder_verified") else "")
                       if holders else "which is linked to another member")
            how = f" {_METHOD[d['method']]}" if d.get("method") in _METHOD else ""
            own = f", whose own link is to {account(d['own_plex_user_id'])}," if d.get("own_plex_user_id") else ""
            msg = f"{who(member)}{own} tried to link {account(target)}{how}, {held_by}. Refused."
            out.append((f"attempt|{target}|{member}", ts, msg))
        elif kind == "takeover":
            displaced = _ids(d, "displaced_discord_user_ids", "displaced_discord_user_id")
            msg = f"{who(member)} took over {account(row.target_id)} by signing in with Plex.{removed(displaced)}"
            out.append((f"takeover|{row.target_id}|{member}", ts, msg))
        elif kind == "joined":
            displaced = _ids(d, "displaced_discord_user_ids", "displaced_discord_user_id")
            msg = (f"{who(member)} joined {account(row.target_id)} by signing in with Plex; it's shared with "
                   f"{whos(_ids(d, 'other_discord_user_ids', ''))}.{removed(displaced)}")
            out.append((f"joined|{row.target_id}|{member}", ts, msg))
        elif kind == "added":
            admin = row.actor.removeprefix("admin:")
            msg = (f"Admin {admin} added {who(d.get('discord_user_id'))} to {account(row.target_id)}, "
                   f"which is also linked to {whos(_ids(d, 'other_discord_user_ids', ''))}.")
            out.append((f"added|{row.target_id}|{d.get('discord_user_id')}", ts, msg))
        else:
            admin = row.actor.removeprefix("admin:")
            msg = (f"Admin {admin} moved {account(row.target_id)} from {who(d.get('previous_discord_user_id'))} "
                   f"to {who(d.get('discord_user_id'))}.")
            out.append((f"moved|{row.target_id}|{d.get('discord_user_id')}", ts, msg))
    return out


def groups(session: Session, min_level: int = logging.WARNING) -> list[Group]:
    """All alert groups, most recent first (resolved ones included, count 0)."""
    now = utcnow()
    resolutions = {r.fingerprint: r for r in session.scalars(select(ErrorResolution))}
    out: dict[str, Group] = {}

    def add(fp: str, level: str, logger: str, message: str, ts: str, exc: str | None = None) -> None:
        g = out.get(fp)
        if g is None:
            g = out[fp] = Group(fp, level, logger, message, first_ts=ts)
        g.message, g.last_ts = message, ts
        g.exc = exc or g.exc
        res = resolutions.get(fp)
        if res is not None and _ts(ts) <= res.until:
            g.hidden += 1
        else:
            g.count += 1

    entries, _ = logcore.buffer().query(min_level=min_level, limit=100_000)
    for e in entries:
        level = "ERROR" if e.level == "CRITICAL" else e.level
        add(fingerprint(e.level, e.logger, e.message), level, e.logger, e.message, e.ts, _exc_summary(e.exc))
    if min_level <= logging.WARNING:
        for key, ts, message in _account_alerts(session):
            fp = hashlib.sha256(f"account|{key}".encode()).hexdigest()[:16]
            add(fp, "WARNING", ACCOUNT_LOGGER, message, ts)

    for fp, g in out.items():
        res = resolutions.get(fp)
        if res is not None:
            g.resolved_by = res.resolved_by
            g.resolved_until = res.until
    # A resolution that has expired and hides nothing that still exists is no longer needed.
    stale = [fp for fp, r in resolutions.items() if r.until < now and fp not in out]
    if stale:
        session.execute(delete(ErrorResolution).where(ErrorResolution.fingerprint.in_(stale)))
    return sorted(out.values(), key=lambda g: g.last_ts, reverse=True)


def visible_counts(session: Session) -> tuple[int, int]:
    """(warnings, errors) not covered by a resolution."""
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
