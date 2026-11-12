"""Admin user management (P6-07). The last active admin can't be removed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from woosarr.api.auth import admin_out
from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.api.schemas import AdminOut, Ok, Password, Username
from woosarr.core import auth
from woosarr.db.models import AdminUser
from woosarr.services import audit

router = APIRouter(prefix="/admins", tags=["admins"], dependencies=[Depends(require_admin)])


class AdminCreateIn(BaseModel):
    username: Username
    password: Password


class AdminPatchIn(BaseModel):
    is_active: bool | None = None
    password: Password | None = None
    unlock: bool = False


def _get(db, admin_id: int) -> AdminUser:
    user = db.get(AdminUser, admin_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin not found")
    return user


def _guard_last_admin(db, user: AdminUser) -> None:
    if user.is_active and auth.active_admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot remove or disable the last active admin")


@router.get("", response_model=list[AdminOut])
def list_admins(db: DbDep) -> list[AdminOut]:
    return [admin_out(u) for u in db.scalars(select(AdminUser).order_by(func.lower(AdminUser.username), AdminUser.username))]


@router.post("", response_model=AdminOut, status_code=201)
def create_admin(body: AdminCreateIn, db: DbDep, principal: AdminDep) -> AdminOut:
    try:
        user = AdminUser(username=body.username, password_hash=auth.hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists") from None
    audit.record(db, principal.actor, "admin_created", "admin", str(user.id), username=user.username)
    return admin_out(user)


@router.patch("/{admin_id}", response_model=AdminOut)
def update_admin(admin_id: int, body: AdminPatchIn, db: DbDep, principal: AdminDep) -> AdminOut:
    user = _get(db, admin_id)
    if body.is_active is False and user.is_active:
        _guard_last_admin(db, user)
        user.is_active = False
        auth.end_all_sessions(db, user.id)
        audit.record(db, principal.actor, "admin_disabled", "admin", str(user.id))
    elif body.is_active is True and not user.is_active:
        user.is_active = True
        audit.record(db, principal.actor, "admin_enabled", "admin", str(user.id))
    if body.password is not None:
        if user.auth_source != "local":
            # Setting one would quietly turn a single sign-on admin into a password admin.
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"{user.username} signs in with single sign-on and has no WooSarr password")
        try:
            user.password_hash = auth.hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        user.auth_source = "local"
        auth.end_all_sessions(db, user.id, keep_hash=principal.session_hash)
        audit.record(db, principal.actor, "admin_password_reset", "admin", str(user.id))
    if body.unlock:
        user.locked_until = None
        user.failed_logins = 0
        audit.record(db, principal.actor, "admin_unlocked", "admin", str(user.id))
    return admin_out(user)


@router.delete("/{admin_id}", response_model=Ok)
def delete_admin(admin_id: int, db: DbDep, principal: AdminDep) -> Ok:
    user = _get(db, admin_id)
    _guard_last_admin(db, user)
    db.delete(user)
    audit.record(db, principal.actor, "admin_deleted", "admin", str(admin_id), username=user.username)
    return Ok()
