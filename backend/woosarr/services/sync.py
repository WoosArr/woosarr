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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime

from sqlalchemy import select

from woosarr.core import app_settings
from woosarr.db.models import ManualGrant, utcnow
from woosarr.db.session import Database
from woosarr.plex_gateway import PlexGateway
from woosarr.services import audit, impact, inventory, invites, self_resolve
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
        # Called (in the worker thread) after every run, e.g. to refresh link-status roles.
        self.after_run: list[Callable[[SyncReport], None]] = []

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
        self._notify(report)
        return report

    def _notify(self, report: SyncReport) -> None:
        for fn in self.after_run:
            try:
                fn(report)
            except Exception:  # a hook must never break syncing
                log.exception("after-run hook failed")

    def drop_role_access(self, plex_user_id: str, reason: str,
                         then: Callable[[], object] | None = None) -> SyncReport:
        """Remove role-granted libraries for one user right now (manual + exempt stay).

        Used before a self-unlink when unlink_policy is drop_role_access; respects
        dry-run. `then` runs inside the same lock (e.g. delete the link), so no
        queued sync can re-grant role access in between.
        """
        with self._run_lock:
            self.running = True
            try:
                report = self._run([plex_user_id], reason, drop_roles=True)
                if then is not None:
                    then()
            finally:
                self.running = False
            self.last_report = report
        self._notify(report)
        return report

    def remove_access(self, plex_user_ids: Iterable[str], library_ids: Iterable[str],
                      actor: str) -> list[dict[str, object]]:
        """Admin action: take libraries away from users now, in Plex.

        Removes matching manual grants, then removes the libraries from each
        user's live shares (complete-set write, N6). For a linked user, libraries
        their Discord roles still grant are kept (the next sync would re-add
        them); the result says so. Respects dry-run; audited per user.
        """
        wanted = frozenset(library_ids)
        results: list[dict[str, object]] = []
        with self._run_lock:
            with self._db.session() as s:
                settings = app_settings.load(s)
                manual_removed: dict[str, list[str]] = {}
                for uid in plex_user_ids:
                    rows = list(s.scalars(select(ManualGrant).where(ManualGrant.plex_user_id == uid,
                                                                    ManualGrant.library_id.in_(wanted))))
                    manual_removed[uid] = sorted(r.library_id for r in rows)
                    for r in rows:
                        s.delete(r)
            gateway = self._gateway()
            with self._db.session() as s:
                snap = load_snapshot(s)
            for uid, manual in manual_removed.items():
                entry: dict[str, object] = {"plex_user_id": uid, "manual_removed": manual, "removed": [],
                                            "kept_by_roles": [], "not_shared": [], "error": None}
                results.append(entry)
                if uid in snap.owner_ids:
                    entry["error"] = "the server owner always has every library"
                    continue
                if gateway is None:
                    entry["error"] = "Plex is not configured"
                    continue
                with self._user_lock(uid):
                    try:
                        current = gateway.get_user_library_ids(uid)
                    except Exception as exc:  # reported per user
                        log.exception("remove access: reading shares failed for user=%s", uid)
                        entry["error"] = f"{type(exc).__name__}: {exc}"
                        continue
                    remove, keep = impact.removal_split(snap, settings, uid, current, wanted)
                    entry.update(removed=sorted(remove), kept_by_roles=sorted(keep),
                                 not_shared=sorted(wanted - current))
                    if remove and not settings.dry_run:
                        try:
                            gateway.set_user_libraries(uid, current - remove)
                        except Exception as exc:  # reported per user
                            log.exception("remove access: write failed for user=%s", uid)
                            entry["error"] = f"{type(exc).__name__}: {exc}"
                            continue
                        with self._db.session() as s:
                            inventory.store_shares(s, uid, current - remove)
                with self._db.session() as s:
                    audit.record(s, actor, "access_removed_admin", "plex_user", uid, dry_run=settings.dry_run,
                                 removed=entry["removed"], kept_by_roles=entry["kept_by_roles"],
                                 manual_removed=manual, not_shared=entry["not_shared"])
        return results

    def restore_access(self, items: Iterable[dict], actor: str) -> list[dict[str, object]]:
        """Undo a 'Remove libraries': give the removed libraries back and recreate
        the manual grants that were deleted. Respects dry-run; audited."""
        results: list[dict[str, object]] = []
        with self._run_lock:
            with self._db.session() as s:
                settings = app_settings.load(s)
                for item in items:
                    for lib in item.get("manual_library_ids") or []:
                        if s.get(ManualGrant, (item["plex_user_id"], lib)) is None:
                            s.add(ManualGrant(plex_user_id=item["plex_user_id"], library_id=lib, granted_by=actor))
            gateway = self._gateway()
            for item in items:
                uid, libs = str(item["plex_user_id"]), frozenset(item.get("library_ids") or [])
                entry: dict[str, object] = {"plex_user_id": uid, "restored": [], "error": None}
                results.append(entry)
                if libs and gateway is None:
                    entry["error"] = "Plex is not configured"
                    continue
                if libs:
                    with self._user_lock(uid):
                        try:
                            current = gateway.get_user_library_ids(uid)
                            missing = libs - current
                            if missing and not settings.dry_run:
                                gateway.set_user_libraries(uid, current | missing)
                                with self._db.session() as s:
                                    inventory.store_shares(s, uid, current | missing)
                            entry["restored"] = sorted(missing)
                        except Exception as exc:  # reported per user
                            log.exception("restore access failed for user=%s", uid)
                            entry["error"] = f"{type(exc).__name__}: {exc}"
                            continue
                with self._db.session() as s:
                    audit.record(s, actor, "access_restored_admin", "plex_user", uid, dry_run=settings.dry_run,
                                 restored=entry["restored"], manual_restored=item.get("manual_library_ids") or [])
        return results

    def _run(self, only: list[str] | None, reason: str, drop_roles: bool = False) -> SyncReport:
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
                    invites.match_pending(s)
                    audit.prune(s, settings.audit_retention_days)
                snap = load_snapshot(s)
        except Exception as exc:  # surface any Plex/DB failure in status, keep worker alive
            log.exception("sync aborted while reading inventory")
            report.errors += 1
            report.error_messages.append(f"inventory: {type(exc).__name__}: {exc}")
            report.finished_at = utcnow()
            return report

        targets = snap.plex_user_ids if only is None else [u for u in only if u in snap.plex_user_ids]
        prefetched: dict[str, frozenset[str]] = {}
        if only is None and hasattr(gateway, "get_all_user_library_ids"):
            try:
                prefetched, read_errors = gateway.get_all_user_library_ids()
            except Exception:  # fall back to per-user reads
                log.exception("batch share read failed; reading users one by one")
                prefetched = {}
            else:
                with self._db.session() as s:
                    for uid, ids in prefetched.items():
                        inventory.store_shares(s, uid, ids)
                # Not an alert yet: if the read on its own fails too, "sync failed for user" is.
                for uid, err in read_errors.items():
                    log.info("share read failed for user=%s, retrying alone: %s", uid, err)
        for uid in targets:
            with self._user_lock(uid):
                try:
                    self._sync_user(gateway, snap, settings, uid, report, prefetched.get(uid), drop_roles)
                except Exception as exc:  # one bad user must not stop the others
                    log.exception("sync failed for user=%s", uid)
                    report.errors += 1
                    report.error_messages.append(f"user {uid}: {type(exc).__name__}: {exc}")
        report.finished_at = utcnow()
        if report.full and not report.errors:
            self_resolve.recovered("sync")
        log.info("sync done checked=%s changed=%s writes=%s drift=%s errors=%s dry_run=%s",
                 report.users_checked, report.users_changed, report.writes,
                 report.skipped_drift, report.errors, report.dry_run)
        return report

    def _sync_user(self, gateway, snap, settings, uid: str, report: SyncReport,
                   current: frozenset[str] | None = None, drop_roles: bool = False) -> None:
        if current is None:
            current = gateway.get_user_library_ids(uid)
            with self._db.session() as s:
                inventory.store_shares(s, uid, current)
        state = user_state(snap, uid, settings, current)
        if state is None:
            reason = "server owner" if uid in snap.owner_ids else "leave-server policy"
            log.debug("user=%s skipped (%s)", uid, reason)
            return
        if drop_roles:
            state = replace(state, role_ids=frozenset())
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
