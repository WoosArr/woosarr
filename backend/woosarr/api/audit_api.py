"""Audit view of permission and account changes (P8-02)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from woosarr.api.deps import DbDep, require_admin
from woosarr.db.models import AuditLog
from woosarr.services import audit_names

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_admin)])


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    dry_run: bool
    # Readable versions (services/audit_names.py): names as they were when the entry was written.
    names: dict[str, str]
    actor_label: str
    target_label: str | None
    details_named: dict[str, Any]


def audit_out(db: Any, r: AuditLog) -> AuditOut:
    # Entries from before names were stored, until the startup backfill reaches them: today's names.
    names = r.names if r.names is not None else audit_names.names_for(
        db, r.actor, r.action, r.target_type, r.target_id, r.details)
    return AuditOut(
        id=r.id, ts=r.ts, actor=r.actor, action=r.action, target_type=r.target_type, target_id=r.target_id,
        details=r.details or {}, dry_run=r.dry_run, names=names,
        actor_label=audit_names.actor_label(r.actor, names),
        target_label=audit_names.target_label(r.action, r.target_type, r.target_id, r.details, names),
        details_named=audit_names.details_named(r.action, r.target_type, r.details, names),
    )


class AuditPage(BaseModel):
    entries: list[AuditOut]
    # Pass as before_id to page back further.
    next_before_id: int | None


@router.get("", response_model=AuditPage)
def list_audit(
    db: DbDep,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=50)] = None,
    target_id: Annotated[str | None, Query(max_length=100)] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditPage:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    rows = list(db.scalars(stmt))
    return AuditPage(
        entries=[audit_out(db, r) for r in rows],
        next_before_id=rows[-1].id if len(rows) == limit else None,
    )
