"""Audit view of permission and account changes (P8-02)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from woosarr.api.deps import DbDep, require_admin
from woosarr.db.models import AuditLog

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
        entries=[AuditOut.model_validate(r, from_attributes=True) for r in rows],
        next_before_id=rows[-1].id if len(rows) == limit else None,
    )
