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
from woosarr.services import audit, error_digest, impact
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
    recent_warnings: int  # occurrences
    recent_errors: int
    open_warnings: int  # problems: entries in Alerts
    open_errors: int


def _count(db, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


@router.get("/status", response_model=StatusOut)
def get_status(request: Request, db: DbDep) -> StatusOut:
    state = request.app.state
    settings = app_settings.load(db)
    plex = state.plex_status()
    discord = state.discord_status()
    # Open alerts: everything not covered by a resolution (services/error_digest.py).
    recent_warnings, recent_errors, open_warnings, open_errors = error_digest.visible_counts(db)
    return StatusOut(
        version=__version__,
        dry_run=settings.dry_run,
        plex=IntegrationStatus(configured=plex["configured"], connected=plex["connected"], detail=plex.get("detail")),
        discord=IntegrationStatus(
            configured=discord["configured"], connected=discord["connected"], detail=discord.get("detail")
        ),
        sync=state.sync.status(),
        counts={
            "plex_users": _count(db, PlexUser, PlexUser.removed.is_(False)),
            # Plex accounts with a linked member (a shared account counts once).
            "linked_users": db.scalar(select(func.count(func.distinct(DiscordLink.plex_user_id)))) or 0,
            "verified_links": db.scalar(select(func.count(func.distinct(DiscordLink.plex_user_id)))
                                        .where(DiscordLink.verified.is_(True))) or 0,
            "libraries": _count(db, Library, Library.removed.is_(False)),
            "exempt_libraries": _count(db, Library, Library.is_exempt.is_(True), Library.removed.is_(False)),
            "roles": _count(db, DiscordRole, DiscordRole.deleted.is_(False)),
            "discord_members": _count(db, DiscordMember, DiscordMember.in_guild.is_(True), DiscordMember.is_bot.is_(False)),
            "audit_entries_24h": _count(db, AuditLog, AuditLog.ts >= utcnow() - timedelta(hours=24)),
        },
        recent_warnings=recent_warnings,
        recent_errors=recent_errors,
        open_warnings=open_warnings,
        open_errors=open_errors,
    )


@router.get("/settings", response_model=AppSettings)
def get_settings(db: DbDep) -> AppSettings:
    return app_settings.load(db)


class SettingsImpact(BaseModel):
    users_gaining: int
    users_losing: int
    users: list[dict[str, Any]]


@router.post("/settings/preview", response_model=SettingsImpact)
def preview_settings(changes: dict[str, Any], db: DbDep) -> SettingsImpact:
    """Who would gain/lose which libraries if these settings were saved (no Plex calls)."""
    try:
        proposed = AppSettings.model_validate(app_settings.load(db).model_dump() | changes)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False, include_context=False)
        ) from None
    users = impact.settings_impact(db, proposed)
    return SettingsImpact(users_gaining=sum(1 for u in users if u["gain"]),
                          users_losing=sum(1 for u in users if u["lose"]), users=users)


@router.patch("/settings", response_model=AppSettings)
def patch_settings(changes: dict[str, Any], request: Request, db: DbDep, principal: AdminDep) -> AppSettings:
    before = app_settings.load(db)
    if changes.get("local_login_enabled") is False and before.local_login_enabled:
        from woosarr.api.oidc import usable_sso_admins

        if usable_sso_admins(db) == 0:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "sign in once with single sign-on (as an admin) before turning password login off")
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
        if "request_command_name" in diff and request.app.state.discord is not None:
            request.app.state.discord.schedule_command_sync()
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
