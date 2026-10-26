"""Alerts that end by themselves leave the dashboard by themselves.

Two kinds (RULES):
- over by the time they're logged: a stall that has ended, a member's button or form that expired
  (left unfinished), a discarded pooled connection, one emoji preview that couldn't load. Resolved
  straight away. Late answers to commands are not: they stay for an admin.
- over when something works again: Seerr answering, the Discord connection healthy, a sync of
  Plex finishing, the event loop running, a reaction post publishing. Services call recovered(key)
  when that happens; occurrences before it are resolved.

Resolutions are the same ErrorResolution rows an admin's *Resolve* makes, with resolved_by
"WooSarr: <reason>", so a repeat after the recovery shows again. The log lines stay; the logs API
marks them resolved, and each automatic resolution is logged (INFO).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from woosarr.db.models import ErrorResolution, utcnow

log = logging.getLogger("woosarr.alerts")

ACTOR = "WooSarr"
# Discord's answers for an interaction it no longer waits for: unknown interaction, unknown webhook
# (the 15-minute follow-up token ran out), unknown message.
_GONE = r"10062|10015|10008|Unknown interaction|Unknown Webhook"


@dataclass(frozen=True)
class Rule:
    logger: str
    message: re.Pattern[str]
    reason: str
    key: str | None = None  # None: over when logged; else resolved once recovered(key) happens after it
    exc: re.Pattern[str] | None = None  # also required in the exception summary

    def match(self, logger: str, message: str, exc: str | None) -> re.Match[str] | None:
        if logger != self.logger:
            return None
        m = self.message.search(message)
        if m and self.exc is not None and not self.exc.search(exc or ""):
            return None
        return m


def _r(logger: str, message: str, reason: str, key: str | None = None, exc: str | None = None) -> Rule:
    return Rule(logger, re.compile(message), reason, key, re.compile(exc) if exc else None)


RULES: tuple[Rule, ...] = (
    # Over when logged.
    _r("woosarr.stall", r"^WooSarr was paused|^WooSarr's event loop was blocked", "it was over when it was logged"),
    # Late answers to commands ("answered too late") deliberately stay until an admin resolves them
    # (owner, 2026-10-25: kept an eye on). Only suggestions Discord dropped while the member typed on.
    _r("discord.app_commands.tree", r"^Ignoring exception in autocomplete",
       "the member typed on; Discord dropped the old suggestions", exc=_GONE),
    _r("discord.ui.view", r"^Ignoring exception in view", "the member's button or menu had expired", exc=_GONE),
    _r("discord.ui.modal", r"^Ignoring exception in modal", "the member's form had expired", exc=_GONE),
    _r("urllib3.connectionpool", r"^Connection pool is full", "harmless: an extra connection was closed"),
    _r("woosarr.emoji", r"^emoji library: image fetch failed", "one preview couldn't load; it's fetched again when needed"),
    # Over when something works again.
    _r("woosarr.stall", r"^WooSarr's event loop is blocked", "the event loop ran again", key="loop"),
    _r("woosarr.seerr", r".", "Seerr answered again", key="seerr"),
    _r("woosarr.requests", r"^request poll stopped", "Seerr answered again", key="seerr"),
    _r("discord.gateway", r"heartbeat blocked|Can't keep up", "the Discord connection is healthy again", key="discord"),
    _r("discord.client", r"^Attempting a reconnect", "the bot reconnected to Discord", key="discord"),
    _r("woosarr.discord", r"^discord connection problem: ", "the bot connected to Discord", key="discord"),
    _r("woosarr.sync", r"^sync aborted while reading inventory|^batch share read failed|^sync run crashed",
       "a later sync finished without errors", key="sync"),
    _r("woosarr.discord", r"^reaction post (\d+) not published", "the post was published", key="reaction_post:{0}"),
    _r("woosarr.app", r"^no admin account yet", "the first admin account was created", key="admin"),
)

_lock = threading.Lock()
_recovered: dict[str, datetime] = {}


def recovered(key: str) -> None:
    """Something works again (see RULES); alerts it explains, from before now, are resolved."""
    with _lock:
        _recovered[key] = utcnow()


def recovered_at(key: str) -> datetime | None:
    with _lock:
        return _recovered.get(key)


def reset() -> None:  # tests
    with _lock:
        _recovered.clear()


def rule_for(logger: str, message: str, exc: str | None) -> tuple[Rule, str | None] | None:
    """The rule an alert falls under and its recovery key (None: over when logged)."""
    for rule in RULES:
        m = rule.match(logger, message, exc)
        if m:
            return rule, rule.key.format(*m.groups()) if rule.key else None
    return None


def apply(session: Session, groups: list) -> int:
    """Resolve the open groups that ended by themselves. Returns how many were resolved."""
    done = 0
    now = utcnow()
    for g in groups:
        if g.count <= 0:
            continue
        found = rule_for(g.logger, g.message, g.exc)
        if found is None:
            continue
        rule, key = found
        if key is None:
            when = now
        else:
            when = recovered_at(key)
            if when is None or when <= datetime.fromisoformat(g.last_ts):
                continue  # not working again since the last occurrence
        # An upsert: the dashboard asks for alerts and status at the same moment, and both resolve the
        # same group (a plain insert made the second request fail on the primary key).
        values = {"fingerprint": g.fingerprint, "level": g.level, "logger": g.logger[:100], "message": g.message[:300],
                  "resolved_at": when, "until": when, "resolved_by": f"{ACTOR}: {rule.reason}"[:100]}
        session.execute(sqlite_insert(ErrorResolution).values(**values).on_conflict_do_update(
            index_elements=[ErrorResolution.fingerprint],
            set_={k: v for k, v in values.items() if k != "fingerprint"}))
        log.info("alert resolved by WooSarr (%s): %s", rule.reason, g.message.splitlines()[0][:200])
        done += 1
    return done
