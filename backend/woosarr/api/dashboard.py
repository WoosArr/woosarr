"""Dashboard: error groups with "mark resolved", widget data, per-admin layout."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import kv
from woosarr.db.models import (
    ApiKey,
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    LibraryState,
    MediaRequest,
    PlexInvite,
    PlexUser,
    RoleLibrary,
    Session,
    utcnow,
)
from woosarr.services import audit, error_digest

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_admin)])
log = logging.getLogger("woosarr.dashboard")

# ---- errors -------------------------------------------------------------------------


class ErrorGroupOut(BaseModel):
    fingerprint: str
    level: Literal["WARNING", "ERROR"]
    logger: str
    message: str
    exc: str | None
    count: int  # shown on the dashboard
    hidden: int  # covered by a resolution
    first_ts: str
    last_ts: str
    resolved_until: datetime | None
    resolved_by: str | None


def _groups(db, level: str) -> list[error_digest.Group]:
    return error_digest.groups(db, logging.getLevelName(level))


@router.get("/errors", response_model=list[ErrorGroupOut])
def list_errors(db: DbDep, level: Annotated[Literal["WARNING", "ERROR"], Query()] = "WARNING") -> list[ErrorGroupOut]:
    """Alerts: warnings/errors and account-takeover events grouped by problem, newest first."""
    return [ErrorGroupOut(**{k: getattr(g, k) for k in ErrorGroupOut.model_fields}) for g in _groups(db, level)]


@router.put("/errors/{fingerprint}/resolved", response_model=ErrorGroupOut)
def resolve_error(fingerprint: Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")], db: DbDep,
                  principal: AdminDep) -> ErrorGroupOut:
    """Hide this problem's occurrences from the dashboard for 24 h."""
    group = next((g for g in _groups(db, "WARNING") if g.fingerprint == fingerprint), None)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such alert")
    row = error_digest.resolve(db, group, principal.actor)
    audit.record(db, principal.actor, "error_resolved", "log_group", fingerprint, level=group.level,
                 logger=group.logger, message=group.message[:200], until=row.until.isoformat())
    db.flush()
    group = next(g for g in _groups(db, "WARNING") if g.fingerprint == fingerprint)
    return ErrorGroupOut(**{k: getattr(group, k) for k in ErrorGroupOut.model_fields})


@router.delete("/errors/{fingerprint}/resolved", status_code=204)
def unresolve_error(fingerprint: Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")], db: DbDep,
                    principal: AdminDep) -> None:
    if not error_digest.unresolve(db, fingerprint):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not resolved")
    audit.record(db, principal.actor, "error_unresolved", "log_group", fingerprint)


# ---- widget data ----------------------------------------------------------------------

DAYS = 14
ACTIVITY_SKIP = ("access_planned", "access_changed", "access_skipped", "login", "logout", "request_status")


class InsightsOut(BaseModel):
    links: dict[str, int]
    activity: list[dict[str, Any]]
    access_by_day: list[dict[str, Any]]
    libraries: list[dict[str, Any]]
    roles: list[dict[str, Any]]  # Discord roles by member count, with linked members and mapped libraries
    invites: dict[str, Any]
    requests: dict[str, Any]
    setup: dict[str, Any]
    security: dict[str, int]


def _day(ts: datetime) -> str:
    return ts.date().isoformat()


@router.get("/dashboard/insights", response_model=InsightsOut)
def insights(request: Request, db: DbDep) -> InsightsOut:
    """Everything the dashboard widgets show, from data WooSarr already keeps."""
    now = utcnow()
    # The server owner counts as a Plex user everywhere on the dashboard (same as the status counts).
    plex = db.scalars(select(PlexUser).where(PlexUser.removed.is_(False))).all()
    owners = {p.plex_user_id for p in plex if p.is_owner}
    ids = {p.plex_user_id for p in plex}
    links = db.execute(select(DiscordLink.plex_user_id, DiscordLink.discord_user_id, DiscordLink.verified)).all()
    # Per Plex account (a shared account counts once): verified if any member is.
    linked_accounts: dict[str, bool] = {}
    for link in links:
        if link.plex_user_id in ids:
            linked_accounts[link.plex_user_id] = linked_accounts.get(link.plex_user_id, False) or link.verified
    linked_discord = {link.discord_user_id for link in links}
    member_roles = db.execute(select(DiscordMember.discord_user_id, DiscordMember.role_ids)
                              .where(DiscordMember.in_guild.is_(True))).all()
    members = [m.discord_user_id for m in member_roles]

    # Access changes per day: applied changes, or planned ones while in dry-run.
    since = now - timedelta(days=DAYS - 1)
    start = since.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {_day(start + timedelta(days=i)): {"date": _day(start + timedelta(days=i)), "changed": 0, "planned": 0,
                                               "users": set()} for i in range(DAYS)}
    for ts, action, target in db.execute(select(AuditLog.ts, AuditLog.action, AuditLog.target_id).where(
            AuditLog.ts >= start, AuditLog.action.in_(("access_changed", "access_planned")))):
        d = days.get(_day(ts))
        if d is not None:
            d["changed" if action == "access_changed" else "planned"] += 1
            d["users"].add(target)
    access_by_day = [{"date": d["date"], "changed": d["changed"], "planned": d["planned"], "users": len(d["users"])}
                     for d in days.values()]

    activity = [{"ts": a.ts, "actor": a.actor, "action": a.action, "target_type": a.target_type,
                 "target_id": a.target_id, "dry_run": a.dry_run}
                for a in db.scalars(select(AuditLog).where(AuditLog.action.not_in(ACTIVITY_SKIP))
                                    .order_by(AuditLog.ts.desc()).limit(12))]

    libs = db.scalars(select(Library).where(Library.removed.is_(False))).all()
    # Shares cover friends only; the owner sees every library of their own server.
    shared = dict(db.execute(select(LibraryState.library_id, func.count())
                             .where(LibraryState.plex_user_id.not_in(owners))
                             .group_by(LibraryState.library_id)).all())
    mapped_libs = set(db.scalars(select(RoleLibrary.library_id)))
    libraries = sorted(({"library_id": lib.library_id, "title": lib.title, "type": lib.type,
                         "users": shared.get(lib.library_id, 0) + len(owners), "exempt": lib.is_exempt,
                         "managed": lib.library_id in mapped_libs} for lib in libs),
                       key=lambda x: (-x["users"], x["title"].lower(), x["title"]))

    pending = db.scalars(select(PlexInvite).where(PlexInvite.status == "pending")
                         .order_by(PlexInvite.created_at.desc())).all()
    invites = {"pending": len(pending),
               "accepted_7d": db.scalar(select(func.count()).select_from(PlexInvite).where(
                   PlexInvite.status == "accepted", PlexInvite.resolved_at >= now - timedelta(days=7))) or 0,
               "recent": [{"identifier": i.identifier, "created_at": i.created_at} for i in pending[:5]]}

    by_status = dict(db.execute(select(MediaRequest.status, func.count()).group_by(MediaRequest.status)).all())
    recent_rows = list(db.scalars(select(MediaRequest).order_by(MediaRequest.created_at.desc()).limit(5)))
    plex_names = dict(db.execute(select(PlexUser.plex_user_id, PlexUser.username).where(
        PlexUser.plex_user_id.in_({r.plex_user_id for r in recent_rows if r.plex_user_id}))).all())
    discord_names = {m.discord_user_id: m.display_name or m.username for m in db.scalars(select(DiscordMember).where(
        DiscordMember.discord_user_id.in_({r.discord_user_id for r in recent_rows})))}
    recent_requests = [{"title": r.title, "year": r.year, "media_type": r.media_type, "status": r.status,
                        "created_at": r.created_at,
                        # Who asked: their Plex account (what Seerr requested as), else their Discord name.
                        "plex_username": plex_names.get(r.plex_user_id) if r.plex_user_id else None,
                        "discord_name": discord_names.get(r.discord_user_id)}
                       for r in recent_rows]
    req_svc = getattr(request.app.state, "requests", None)
    requests = {"configured": bool(req_svc and req_svc.configured()), "by_status": by_status,
                "last_7d": db.scalar(select(func.count()).select_from(MediaRequest).where(
                    MediaRequest.created_at >= now - timedelta(days=7))) or 0,
                "recent": recent_requests}

    roles = db.scalars(select(DiscordRole).where(DiscordRole.deleted.is_(False), DiscordRole.managed.is_(False))).all()
    mapped_roles = set(db.scalars(select(RoleLibrary.role_id)))
    libs_per_role: dict[str, int] = {}
    for role_id in db.scalars(select(RoleLibrary.role_id)):
        libs_per_role[role_id] = libs_per_role.get(role_id, 0) + 1
    in_role: dict[str, int] = {}
    linked_in_role: dict[str, int] = {}
    for m in member_roles:
        for role_id in set(m.role_ids or ()):
            in_role[role_id] = in_role.get(role_id, 0) + 1
            if m.discord_user_id in linked_discord:
                linked_in_role[role_id] = linked_in_role.get(role_id, 0) + 1
    roles_out = sorted(({"role_id": r.role_id, "name": r.name, "color": r.color, "members": in_role.get(r.role_id, 0),
                         "linked": linked_in_role.get(r.role_id, 0), "libraries": libs_per_role.get(r.role_id, 0)}
                        for r in roles), key=lambda x: (-x["members"], x["name"].lower()))

    setup = {"unmanaged_libraries": sum(1 for lib in libs if not lib.is_exempt and lib.library_id not in mapped_libs),
             "exempt_libraries": sum(1 for lib in libs if lib.is_exempt),
             "roles_mapped": sum(1 for r in roles if r.role_id in mapped_roles),
             "roles_unmapped": sum(1 for r in roles if r.role_id not in mapped_roles)}

    security = {
        "failed_logins_24h": db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "login_failed", AuditLog.ts >= now - timedelta(hours=24))) or 0,
        "sessions": db.scalar(select(func.count()).select_from(Session).where(Session.expires_at > now)) or 0,
        "api_keys": db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))) or 0,
    }

    return InsightsOut(
        links={"plex_users": len(plex), "linked": len(linked_accounts),
               "verified": sum(1 for v in linked_accounts.values() if v),
               "unverified": sum(1 for v in linked_accounts.values() if not v),
               "unlinked": len(plex) - len(linked_accounts),
               "members": len(members), "members_unlinked": sum(1 for m in members if m not in linked_discord)},
        activity=activity, access_by_day=access_by_day, libraries=libraries, roles=roles_out, invites=invites, requests=requests,
        setup=setup, security=security)


# ---- layout ---------------------------------------------------------------------------

WIDGETS = (
    "integrations", "stat_plex_users", "stat_linked", "stat_libraries", "stat_roles", "stat_requests",
    "stat_invites", "stat_invites_accepted", "errors", "last_sync", "pending", "links", "access_chart", "activity", "libraries",
    "requests", "invites", "setup", "security", "roles",
)
DEFAULT_LAYOUT: list[dict[str, Any]] = [
    {"id": "integrations", "size": "l"},
    {"id": "stat_plex_users", "size": "s"}, {"id": "stat_linked", "size": "s"},
    {"id": "stat_libraries", "size": "s"}, {"id": "stat_roles", "size": "s"},
    {"id": "errors", "size": "l"},
    {"id": "last_sync", "size": "m"}, {"id": "pending", "size": "m"},
    {"id": "links", "size": "m"}, {"id": "access_chart", "size": "m"},
    {"id": "activity", "size": "m"}, {"id": "libraries", "size": "m"},
    {"id": "requests", "size": "m", "hidden": True}, {"id": "invites", "size": "m", "hidden": True},
    {"id": "setup", "size": "m", "hidden": True}, {"id": "security", "size": "m", "hidden": True},
    {"id": "stat_requests", "size": "s", "hidden": True}, {"id": "stat_invites", "size": "s", "hidden": True},
    {"id": "stat_invites_accepted", "size": "s", "hidden": True},
    {"id": "roles", "size": "m", "hidden": True},
]


class WidgetIn(BaseModel):
    id: Literal[WIDGETS]  # type: ignore[valid-type]
    size: Literal["xs", "s", "m", "l"] = "m"  # eighth, quarter, half, full width
    # How a widget with chartable data is drawn: its usual form, a circle (donut) or bars.
    view: Literal["default", "donut", "bar"] = "default"
    hidden: bool = False


class LayoutIn(BaseModel):
    widgets: Annotated[list[WidgetIn], Field(max_length=len(WIDGETS))]


class LayoutOut(BaseModel):
    widgets: list[WidgetIn]
    custom: bool  # False = WooSarr's default


def _key(principal) -> str:
    return f"dashboard.layout.{principal.user.id}"


def _complete(widgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Known widgets only, each once; widgets added in a newer version appear hidden at the end."""
    seen, out = set(), []
    for w in widgets:
        if w.get("id") in WIDGETS and w["id"] not in seen:
            seen.add(w["id"])
            out.append(w)
    out += [dict(w, hidden=True) for w in DEFAULT_LAYOUT if w["id"] not in seen]
    return out


@router.get("/dashboard/layout", response_model=LayoutOut)
def get_layout(db: DbDep, principal: AdminDep) -> LayoutOut:
    stored = kv.get(db, _key(principal))
    return LayoutOut(widgets=_complete(stored or DEFAULT_LAYOUT), custom=stored is not None)


@router.put("/dashboard/layout", response_model=LayoutOut)
def put_layout(body: LayoutIn, db: DbDep, principal: AdminDep) -> LayoutOut:
    ids = [w.id for w in body.widgets]
    if len(ids) != len(set(ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "each widget may appear only once")
    widgets = _complete([w.model_dump() for w in body.widgets])
    kv.put(db, _key(principal), widgets)
    return LayoutOut(widgets=widgets, custom=True)


@router.delete("/dashboard/layout", response_model=LayoutOut)
def reset_layout(db: DbDep, principal: AdminDep) -> LayoutOut:
    kv.delete(db, _key(principal))
    return LayoutOut(widgets=_complete(DEFAULT_LAYOUT), custom=False)
