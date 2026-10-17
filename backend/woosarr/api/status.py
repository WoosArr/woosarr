"""Dashboard status, app settings and sync controls (P7-04, P7-05, P5-04, D16)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from woosarr import __version__
from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import app_settings
from woosarr.core import logs as logcore
from woosarr.core import secret_store as names
from woosarr.core.app_settings import AppSettings
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    PlexUser,
    utcnow,
)
from woosarr.services import audit
from woosarr.services.access import load_snapshot, user_state
from woosarr.sync.engine import plan_for_user

router = APIRouter(tags=["status"], dependencies=[Depends(require_admin)])


class IntegrationStatus(BaseModel):
    configured: bool
    connected: bool
    detail: str | None = None


class StatusOut(BaseModel):
    version: str
    dry_run: bool
    plex: IntegrationStatus
    discord: IntegrationStatus
    sync: dict[str, Any]
    counts: dict[str, int]
    recent_warnings: int
    recent_errors: int


def _count(db, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


@router.get("/status", response_model=StatusOut)
def get_status(request: Request, db: DbDep) -> StatusOut:
    state = request.app.state
    settings = app_settings.load(db)
    plex_gw = state.plex_gateway()
    discord = state.discord_status()
    entries, _ = logcore.buffer().query(min_level=logging.WARNING, limit=5000)
    cutoff = (utcnow() - timedelta(hours=24)).isoformat()
    recent = [e for e in entries if e.ts >= cutoff]
    return StatusOut(
        version=__version__,
        dry_run=settings.dry_run,
        plex=IntegrationStatus(
            configured=state.secrets.is_set(db, names.PLEX_TOKEN) or plex_gw is not None,
            connected=plex_gw is not None,
            detail=None if plex_gw is not None else "Plex integration not set up yet",
        ),
        discord=IntegrationStatus(**discord),
        sync=state.sync.status(),
        counts={
            "plex_users": _count(db, PlexUser, PlexUser.removed.is_(False)),
            "linked_users": _count(db, DiscordLink),
            "verified_links": _count(db, DiscordLink, DiscordLink.verified.is_(True)),
            "libraries": _count(db, Library, Library.removed.is_(False)),
            "exempt_libraries": _count(db, Library, Library.is_exempt.is_(True), Library.removed.is_(False)),
            "roles": _count(db, DiscordRole, DiscordRole.deleted.is_(False)),
            "discord_members": _count(db, DiscordMember, DiscordMember.in_guild.is_(True)),
            "audit_entries_24h": _count(db, AuditLog, AuditLog.ts >= utcnow() - timedelta(hours=24)),
        },
        recent_warnings=sum(1 for e in recent if e.level == "WARNING"),
        recent_errors=sum(1 for e in recent if e.level in ("ERROR", "CRITICAL")),
    )


@router.get("/settings", response_model=AppSettings)
def get_settings(db: DbDep) -> AppSettings:
    return app_settings.load(db)


@router.patch("/settings", response_model=AppSettings)
def patch_settings(changes: dict[str, Any], request: Request, db: DbDep, principal: AdminDep) -> AppSettings:
    before = app_settings.load(db)
    try:
        after = app_settings.update(db, changes)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False, include_context=False)
        ) from None
    diff = {k: [getattr(before, k), v] for k, v in after.model_dump().items() if getattr(before, k) != v}
    if diff:
        audit.record(db, principal.actor, "settings_changed", "settings", "app", changes=diff)
        apply_runtime_settings(request.app, after)
        if "dry_run" in diff or "leave_policy" in diff or "ignore_unverified_links" in diff:
            request.app.state.sync.trigger(None, "settings changed")
    return after


def apply_runtime_settings(app, settings: AppSettings) -> None:
    level = settings.log_level or app.state.settings.log_level
    logging.getLogger().setLevel(level)


class SyncIn(BaseModel):
    plex_user_ids: list[str] | None = None


class SyncQueued(BaseModel):
    queued: Literal["all", "users"]
    count: int | None = None


@router.post("/sync", response_model=SyncQueued, status_code=202)
def sync_now(body: SyncIn, request: Request, principal: AdminDep) -> SyncQueued:
    reason = f"sync now by {principal.actor}"
    if body.plex_user_ids is None:
        request.app.state.sync.trigger(None, reason, delay=0)
        return SyncQueued(queued="all")
    request.app.state.sync.trigger(body.plex_user_ids, reason, delay=0)
    return SyncQueued(queued="users", count=len(body.plex_user_ids))


class PlanOut(BaseModel):
    plex_user_id: str
    username: str
    add: list[str]
    remove: list[str]
    remove_all: bool


@router.get("/sync/preview", response_model=list[PlanOut])
def sync_preview(db: DbDep) -> list[PlanOut]:
    """What a reconcile would change right now, from the last observed Plex state."""
    settings = app_settings.load(db)
    snap = load_snapshot(db)
    names_by_id = dict(db.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    out = []
    for uid in snap.plex_user_ids:
        current = snap.cached_shares.get(uid, frozenset())
        st = user_state(snap, uid, settings, current)
        plan = plan_for_user(st, snap.role_map, snap.exempt) if st else None
        if plan and plan.changed:
            out.append(PlanOut(plex_user_id=uid, username=names_by_id.get(uid, uid), add=sorted(plan.add),
                               remove=sorted(plan.remove), remove_all=plan.remove_all))
    return out
