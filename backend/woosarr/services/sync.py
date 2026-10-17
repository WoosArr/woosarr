"""Sync orchestration (plan P5-02..P5-05, P3-07, D8, D12).

* One worker thread runs every Plex call, so slow Plex never blocks the API
  event loop or the Discord heartbeat.
* trigger() debounces per user (bursts of role events collapse into one run);
  trigger(None) queues a full reconcile. A periodic full reconcile runs every
  `reconcile_interval_minutes`.
* A global lock serialises runs (no two reconciles at once) and per-user locks
  guard any direct run_now() calls; running twice makes no changes the second
  time because plans are computed from fresh live state (idempotent).
* Every planned change is audited with who/what/why/dry_run.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from woosarr.core import app_settings
from woosarr.db.models import utcnow
from woosarr.db.session import Database
from woosarr.plex_gateway import PlexGateway
from woosarr.services import audit, inventory
from woosarr.services.access import load_snapshot, user_state
from woosarr.sync.apply import apply_plan
from woosarr.sync.engine import Plan, plan_for_user

log = logging.getLogger("woosarr.sync")

DEBOUNCE_SECONDS = 5.0
STARTUP_DELAY_SECONDS = 15.0


@dataclass
class SyncReport:
    reason: str
    full: bool
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    users_checked: int = 0
    users_changed: int = 0
    writes: int = 0
    skipped_drift: int = 0
    errors: int = 0
    skipped: str | None = None  # why the whole run was skipped
    error_messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SyncService:
    def __init__(
        self,
        db: Database,
        gateway: Callable[[], PlexGateway | None],
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._debounce = debounce_seconds
        self._run_lock = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        self._user_locks_guard = threading.Lock()
        self._cond = threading.Condition()
        self._pending_users: dict[str, float] = {}
        self._pending_full_at: float | None = None
        self._pending_reasons: list[str] = []
        self._next_periodic = time.monotonic() + STARTUP_DELAY_SECONDS
        self._stop = False
        self._thread: threading.Thread | None = None
        self.running = False
        self.last_report: SyncReport | None = None

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="woosarr-sync", daemon=True)
            self._thread.start()
            log.info("sync worker started")

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ---- triggers ---------------------------------------------------------

    def trigger(self, plex_user_ids: Iterable[str] | None, reason: str, delay: float | None = None) -> None:
        due = time.monotonic() + (self._debounce if delay is None else delay)
        with self._cond:
            if plex_user_ids is None:
                self._pending_full_at = min(self._pending_full_at or due, due)
            else:
                for uid in plex_user_ids:
                    # Debounce: keep pushing the run out while events keep coming.
                    self._pending_users[uid] = due
            self._pending_reasons.append(reason)
            self._cond.notify_all()
        log.debug("sync queued users=%s reason=%s", "all" if plex_user_ids is None else list(plex_user_ids), reason)

    def status(self) -> dict[str, object]:
        with self._cond:
            pending_users = len(self._pending_users)
            full_pending = self._pending_full_at is not None
            next_in = max(0.0, self._next_periodic - time.monotonic())
        return {
            "running": self.running,
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "pending_users": pending_users,
            "full_reconcile_pending": full_pending,
            "next_full_reconcile_in_seconds": round(next_in),
            "plex_configured": self._gateway() is not None,
            "last_run": self.last_report.to_dict() if self.last_report else None,
        }

    # ---- worker -----------------------------------------------------------

    def _interval_seconds(self) -> float:
        with self._db.session() as s:
            return app_settings.load(s).reconcile_interval_minutes * 60.0

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stop:
                    return
                now = time.monotonic()
                full_due = (self._pending_full_at is not None and self._pending_full_at <= now) or (
                    self._next_periodic <= now
                )
                due_users = [u for u, t in self._pending_users.items() if t <= now]
                if not full_due and not due_users:
                    waits = [t - now for t in self._pending_users.values()]
                    waits.append(self._next_periodic - now)
                    if self._pending_full_at is not None:
                        waits.append(self._pending_full_at - now)
                    self._cond.wait(timeout=max(0.05, min(waits)))
                    continue
                reasons = sorted(set(self._pending_reasons))
                if self._next_periodic <= now:
                    reasons.insert(0, "periodic")
                if full_due:
                    self._pending_users.clear()
                    self._pending_full_at = None
                else:
                    for u in due_users:
                        del self._pending_users[u]
                self._pending_reasons = []
            reason = ", ".join(reasons) or "periodic"
            try:
                self.run_now(None if full_due else due_users, reason)
            # The worker must survive anything a run throws; run_now logs it.
            except Exception:
                log.exception("sync run crashed")
            if full_due:
                try:
                    interval = self._interval_seconds()
                except Exception:  # DB hiccup: retry soon rather than die
                    log.exception("could not read reconcile interval")
                    interval = 60.0
                with self._cond:
                    self._next_periodic = time.monotonic() + interval

    # ---- a run --------------------------------------------------------------

    def _user_lock(self, uid: str) -> threading.Lock:
        with self._user_locks_guard:
            return self._user_locks.setdefault(uid, threading.Lock())

    def run_now(self, plex_user_ids: Iterable[str] | None, reason: str) -> SyncReport:
        """Reconcile now, in the calling thread. None = everyone (full)."""
        full = plex_user_ids is None
        with self._run_lock:
            self.running = True
            try:
                report = self._run(None if full else list(plex_user_ids or ()), reason)
            finally:
                self.running = False
            self.last_report = report
            return report

    def _run(self, only: list[str] | None, reason: str) -> SyncReport:
        with self._db.session() as s:
            settings = app_settings.load(s)
        report = SyncReport(reason=reason, full=only is None, dry_run=settings.dry_run, started_at=utcnow())
        gateway = self._gateway()
        if gateway is None:
            report.skipped = "Plex is not configured"
            report.finished_at = utcnow()
            log.info("sync skipped: Plex is not configured (reason=%s)", reason)
            return report

        log.info("sync start full=%s users=%s dry_run=%s reason=%s",
                 report.full, "all" if only is None else len(only), settings.dry_run, reason)
        try:
            with self._db.session() as s:
                if only is None:
                    inv = inventory.refresh_all(s, gateway)
                    log.info("inventory libraries=%s users=%s (removed libs=%s users=%s)",
                             inv.libraries, inv.users, inv.libraries_removed, inv.users_removed)
                    audit.prune(s, settings.audit_retention_days)
                snap = load_snapshot(s)
        except Exception as exc:  # surface any Plex/DB failure in status, keep worker alive
            log.exception("sync aborted while reading inventory")
            report.errors += 1
            report.error_messages.append(f"inventory: {type(exc).__name__}: {exc}")
            report.finished_at = utcnow()
            return report

        targets = snap.plex_user_ids if only is None else [u for u in only if u in snap.plex_user_ids]
        for uid in targets:
            with self._user_lock(uid):
                try:
                    self._sync_user(gateway, snap, settings, uid, report)
                except Exception as exc:  # one bad user must not stop the others
                    log.exception("sync failed for user=%s", uid)
                    report.errors += 1
                    report.error_messages.append(f"user {uid}: {type(exc).__name__}: {exc}")
        report.finished_at = utcnow()
        log.info("sync done checked=%s changed=%s writes=%s drift=%s errors=%s dry_run=%s",
                 report.users_checked, report.users_changed, report.writes,
                 report.skipped_drift, report.errors, report.dry_run)
        return report

    def _sync_user(self, gateway, snap, settings, uid: str, report: SyncReport) -> None:
        current = gateway.get_user_library_ids(uid)
        with self._db.session() as s:
            inventory.store_shares(s, uid, current)
        state = user_state(snap, uid, settings, current)
        if state is None:
            log.debug("user=%s skipped by leave-server policy", uid)
            return
        plan = plan_for_user(state, snap.role_map, snap.exempt)
        if plan is None:  # not linked (N5)
            return
        report.users_checked += 1
        if not plan.changed:
            return
        report.users_changed += 1
        result = apply_plan(gateway, plan, settings.dry_run)
        if result.applied:
            report.writes += 1
        if result.skipped_reason == "state drifted":
            report.skipped_drift += 1
        with self._db.session() as s:
            if result.applied:
                inventory.store_shares(s, uid, plan.target)
            _audit_plan(s, plan, result.skipped_reason, report.reason, settings.dry_run)


def _audit_plan(session, plan: Plan, skipped: str | None, reason: str, dry_run: bool) -> None:
    audit.record(
        session,
        "system",
        "access_planned" if dry_run else ("access_skipped" if skipped else "access_changed"),
        "plex_user",
        plan.plex_user_id,
        dry_run=dry_run,
        add=sorted(plan.add),
        remove=sorted(plan.remove),
        remove_all=plan.remove_all,
        reasons=dict(plan.reasons),
        trigger=reason,
        skipped=skipped,
    )
