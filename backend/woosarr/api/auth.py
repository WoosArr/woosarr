"""Login, logout, session status and password change (P6-01)."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from woosarr.api.deps import AdminDep, DbDep, client_ip, current_principal
from woosarr.api.schemas import AdminOut, Ok, Password, Username
from woosarr.core import app_settings, auth
from woosarr.db.models import AdminUser, utcnow
from woosarr.services import audit

log = logging.getLogger("woosarr.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


def admin_out(user: AdminUser) -> AdminOut:
    locked = bool(user.locked_until and user.locked_until > utcnow())
    return AdminOut(
        id=user.id,
        username=user.username,
        auth_source=user.auth_source,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        locked=locked,
    )


class AuthStatus(BaseModel):
    setup_required: bool
    setup_code_required: bool
    local_login_enabled: bool
    authenticated: bool
    user: AdminOut | None = None
    # Send back as X-CSRF-Token on POST/PUT/PATCH/DELETE.
    csrf_token: str | None = None


class LoginIn(BaseModel):
    username: Username
    password: Password


class LoginOut(BaseModel):
    user: AdminOut
    csrf_token: str


class PasswordChangeIn(BaseModel):
    current_password: Password
    new_password: Password


def set_session_cookie(request: Request, response: Response, value: str, max_age: timedelta) -> None:
    mode = request.app.state.settings.cookie_secure
    secure = mode == "always" or (mode == "auto" and request.url.scheme == "https")
    response.set_cookie(
        auth.SESSION_COOKIE,
        value,
        max_age=int(max_age.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def start_session(request: Request, response: Response, db, user: AdminUser) -> str:
    settings = request.app.state.settings
    max_age = timedelta(days=settings.session_max_days)
    new = auth.create_session(db, user, max_age, client_ip(request), request.headers.get("user-agent"))
    set_session_cookie(request, response, new.cookie_value, max_age)
    return new.csrf_token


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request, db: DbDep) -> AuthStatus:
    settings = request.app.state.settings
    try:
        principal = current_principal(request, db)
    except HTTPException:
        principal = None
    return AuthStatus(
        setup_required=not auth.admin_exists(db),
        setup_code_required=settings.setup_code_required,
        local_login_enabled=app_settings.load(db).local_login_enabled,
        authenticated=principal is not None,
        user=admin_out(principal.user) if principal else None,
        csrf_token=principal.csrf_token if principal else None,
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response, db: DbDep) -> LoginOut:
    ip = client_ip(request)
    # Only failed attempts count: signing in often (several devices behind one
    # address) must not lock anyone out; guessing passwords must.
    if request.app.state.login_limiter.blocked(ip):
        log.warning("login rate limit hit ip=%s", ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts; try again later")
    if not app_settings.load(db).local_login_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local login is disabled; use single sign-on")
    try:
        user = auth.authenticate(db, body.username, body.password)
    except auth.AuthError as exc:
        # Commit the failed-login counter / lockout, then answer.
        request.app.state.login_limiter.record(ip)
        db.commit()
        log.warning("login failed username=%s ip=%s reason=%s", body.username, ip, exc)
        audit.record(db, f"ip:{ip}", "login_failed", "admin", body.username)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None
    old = request.cookies.get(auth.SESSION_COOKIE)
    if old:
        auth.end_session(db, old)
    csrf = start_session(request, response, db, user)
    audit.record(db, f"admin:{user.username}", "login", "admin", str(user.id), ip=ip)
    return LoginOut(user=admin_out(user), csrf_token=csrf)


@router.post("/logout", response_model=Ok)
def logout(request: Request, response: Response, db: DbDep, principal: AdminDep) -> Ok:
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if cookie:
        auth.end_session(db, cookie)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    audit.record(db, principal.actor, "logout", "admin", str(principal.user.id))
    return Ok()


@router.post("/password", response_model=Ok)
def change_password(body: PasswordChangeIn, request: Request, db: DbDep, principal: AdminDep) -> Ok:
    user = db.get(AdminUser, principal.user.id)
    if user is None or user.auth_source != "local":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password login is not used for this account")
    if request.app.state.login_limiter.blocked(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later")
    try:
        auth.authenticate(db, user.username, body.current_password)
    except auth.AuthError:
        request.app.state.login_limiter.record(client_ip(request))
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is wrong") from None
    try:
        user.password_hash = auth.hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    # Everyone else holding this account's sessions is signed out.
    auth.end_all_sessions(db, user.id, keep_hash=principal.session_hash)
    audit.record(db, principal.actor, "password_changed", "admin", str(user.id))
    return Ok()
