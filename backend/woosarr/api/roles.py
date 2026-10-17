"""Roles page API: role -> library mapping with impact preview (P7-02)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import app_settings
from woosarr.db.models import DiscordRole, Library, PlexUser, RoleLibrary
from woosarr.services import audit
from woosarr.services.access import load_snapshot, role_holders, user_state
from woosarr.sync.engine import plan_for_user

router = APIRouter(prefix="/roles", tags=["roles"], dependencies=[Depends(require_admin)])


class RoleOut(BaseModel):
    role_id: str
    name: str
    color: int
    position: int
    managed: bool
    deleted: bool
    library_ids: list[str]
    linked_holders: int


class RoleLibrariesIn(BaseModel):
    library_ids: list[str] = Field(max_length=500)


class UserImpact(BaseModel):
    plex_user_id: str
    username: str
    gain: list[str]
    lose: list[str]


class ImpactOut(BaseModel):
    users_gaining: int
    users_losing: int
    users: list[UserImpact]


def _known_role(db, role_id: str) -> DiscordRole | None:
    return db.get(DiscordRole, role_id)


@router.get("", response_model=list[RoleOut])
def list_roles(db: DbDep) -> list[RoleOut]:
    snap = load_snapshot(db)
    holders = role_holders(snap)
    roles = {r.role_id: r for r in db.scalars(select(DiscordRole))}
    out = [
        RoleOut(role_id=r.role_id, name=r.name, color=r.color, position=r.position, managed=r.managed,
                deleted=r.deleted, library_ids=sorted(snap.role_map.get(r.role_id, ())),
                linked_holders=len(holders.get(r.role_id, ())))
        for r in roles.values()
    ]
    # Mappings for roles the cache no longer knows (deleted while the bot was offline).
    for role_id, libs in snap.role_map.items():
        if role_id not in roles:
            out.append(RoleOut(role_id=role_id, name=f"(unknown role {role_id})", color=0, position=-1,
                               managed=False, deleted=True, library_ids=sorted(libs), linked_holders=0))
    return sorted(out, key=lambda r: (-r.position, r.name.lower()))


def _impact(db, role_id: str, library_ids: set[str]) -> ImpactOut:
    settings = app_settings.load(db)
    snap = load_snapshot(db)
    proposed = dict(snap.role_map)
    proposed[role_id] = frozenset(library_ids)
    if not library_ids:
        proposed.pop(role_id, None)
    names = dict(db.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    users = []
    for uid in snap.plex_user_ids:
        current = snap.cached_shares.get(uid, frozenset())
        st = user_state(snap, uid, settings, current)
        if st is None:
            continue
        before = plan_for_user(st, snap.role_map, snap.exempt)
        after = plan_for_user(st, proposed, snap.exempt)
        if before is None or after is None:
            continue
        gain, lose = after.target - before.target, before.target - after.target
        if gain or lose:
            users.append(UserImpact(plex_user_id=uid, username=names.get(uid, uid), gain=sorted(gain), lose=sorted(lose)))
    return ImpactOut(users_gaining=sum(1 for u in users if u.gain), users_losing=sum(1 for u in users if u.lose),
                     users=users)


def _validate_libraries(db, library_ids: list[str]) -> set[str]:
    wanted = set(library_ids)
    known = set(db.scalars(select(Library.library_id).where(Library.library_id.in_(wanted))))
    if wanted - known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown libraries: {sorted(wanted - known)}")
    return wanted


@router.post("/{role_id}/preview", response_model=ImpactOut)
def preview_role(role_id: str, body: RoleLibrariesIn, db: DbDep) -> ImpactOut:
    """Who would gain/lose what if this role mapped to exactly these libraries."""
    return _impact(db, role_id, _validate_libraries(db, body.library_ids))


@router.put("/{role_id}/libraries", response_model=ImpactOut)
def set_role_libraries(role_id: str, body: RoleLibrariesIn, request: Request, db: DbDep, principal: AdminDep) -> ImpactOut:
    wanted = _validate_libraries(db, body.library_ids)
    role = _known_role(db, role_id)
    if role is None and wanted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown role")
    impact = _impact(db, role_id, wanted)
    before = set(db.scalars(select(RoleLibrary.library_id).where(RoleLibrary.role_id == role_id)))
    if before == wanted:
        return impact
    db.execute(delete(RoleLibrary).where(RoleLibrary.role_id == role_id))
    for lib in sorted(wanted):
        db.add(RoleLibrary(role_id=role_id, library_id=lib))
    audit.record(db, principal.actor, "role_mapping_changed", "role", role_id,
                 role_name=role.name if role else None, before=sorted(before), after=sorted(wanted),
                 users_gaining=impact.users_gaining, users_losing=impact.users_losing)
    # A mapping change can move the managed universe, so reconcile everyone.
    request.app.state.sync.trigger(None, f"role {role_id} mapping changed by {principal.actor}")
    return impact
