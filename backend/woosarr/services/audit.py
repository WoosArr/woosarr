"""Audit trail of permission and account changes (plan P5-02, P8-02, D4a)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from woosarr.db.models import AuditLog, utcnow
from woosarr.services import audit_names

log = logging.getLogger("woosarr.audit")


def record(
    session: Session,
    actor: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    dry_run: bool = False,
    **details: Any,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        dry_run=dry_run,
        names=audit_names.names_for(session, actor, action, target_type, target_id, details),
    )
    session.add(entry)
    # Mirror into the app log so remote diagnosis via /api/v1/logs sees it too.
    log.info(
        "audit actor=%s action=%s target=%s:%s dry_run=%s details=%s",
        actor, action, target_type, target_id, dry_run, details,
    )
    return entry


def prune(session: Session, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    result = session.execute(delete(AuditLog).where(AuditLog.ts < cutoff))
    return result.rowcount or 0
