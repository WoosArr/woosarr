"""First-run admin creation (P6-06). Public only while no admin exists."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from woosarr.api.auth import LoginOut, admin_out, start_session
from woosarr.api.deps import DbDep, client_ip
from woosarr.api.schemas import Password, Username
from woosarr.core import auth
from woosarr.db.models import AdminUser, Setting
from woosarr.services import audit

log = logging.getLogger("woosarr.setup")
router = APIRouter(prefix="/setup", tags=["setup"])

# Settings row whose primary key makes the claim atomic across transactions.
CLAIM_KEY = "setup.first_admin_claimed"
_claim_lock = threading.Lock()


class SetupAdminIn(BaseModel):
    username: Username
    password: Password
    setup_code: str | None = None


def _gone() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "setup already completed")


@router.post("/admin", response_model=LoginOut)
def create_first_admin(body: SetupAdminIn, request: Request, response: Response, db: DbDep) -> LoginOut:
    state = request.app.state
    ip = client_ip(request)
    if not state.setup_limiter.hit(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later")
    with _claim_lock:
        if auth.admin_exists(db) or db.get(Setting, CLAIM_KEY) is not None:
            raise _gone()
        code = state.setup_code
        if state.settings.setup_code_required and (code is None or not code.matches(body.setup_code)):
            log.warning("first-run setup rejected: wrong setup code ip=%s", ip)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "wrong setup code (see the container log)")
        try:
            password_hash = auth.hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        try:
            db.add(Setting(key=CLAIM_KEY, value={"by": body.username}))
            user = AdminUser(username=body.username, password_hash=password_hash, auth_source="local")
            db.add(user)
            db.flush()
            csrf = start_session(request, response, db, user)
            audit.record(db, f"admin:{user.username}", "first_admin_created", "admin", str(user.id), ip=ip)
            db.commit()
        except (IntegrityError, OperationalError):
            db.rollback()
            raise _gone() from None
        state.setup_code = None
    log.info("first-run setup complete; admin %s created", user.username)
    return LoginOut(user=admin_out(user), csrf_token=csrf)
