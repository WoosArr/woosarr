"""Shared route dependencies: DB session and the admin gate (N10).

require_admin is the single gate for every non-public route.
tests/test_app.py fails if a route outside PUBLIC_PATHS is registered without it.

Accepted credentials:
  * session cookie (browser). Unsafe methods also need X-CSRF-Token.
  * `Authorization: Bearer wsk_...` API key. `read` keys may only use GET/HEAD.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from woosarr.core import auth
from woosarr.db.models import AdminUser

# Routes reachable without auth (N10). The setup route checks for itself that
# no admin exists yet. SAML metadata/ACS join in P6-05.
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/v1/health",
        "/api/v1/auth/status",
        "/api/v1/auth/login",
        "/api/v1/setup/admin",
    }
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.db.session() as session:
        yield session


# scope="function": commit *before* the response is sent. With the default
# ("request") the commit runs after the client already has its answer, so a
# fast follow-up request (e.g. the auth status check right after login) can
# read the database before the new session row exists.
DbDep = Annotated[Session, Depends(get_session, scope="function")]


@dataclass(frozen=True)
class Principal:
    user: AdminUser
    via: str  # "session" | "api_key"
    session_hash: str | None = None
    csrf_token: str | None = None

    @property
    def actor(self) -> str:
        return f"admin:{self.user.username}"


def _unauthorized(detail: str = "authentication required") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def current_principal(request: Request, db: DbDep) -> Principal | None:
    """Who is calling, or None. Does not enforce anything by itself."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        found = auth.resolve_api_key(db, header[7:].strip())
        if found is None:
            raise _unauthorized("invalid API key")
        key, user = found
        if key.scope == "read" and request.method not in SAFE_METHODS:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "read-only API key")
        return Principal(user, "api_key")

    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if not cookie:
        return None
    settings = request.app.state.settings
    found_session = auth.resolve_session(db, cookie, timedelta(hours=settings.session_idle_hours))
    if found_session is None:
        return None
    row, user = found_session
    return Principal(user, "session", row.id_hash, row.csrf_token)


def require_admin(
    request: Request, principal: Annotated[Principal | None, Depends(current_principal)]
) -> Principal:
    if principal is None:
        raise _unauthorized()
    needs_csrf = principal.via == "session" and request.method not in SAFE_METHODS
    if needs_csrf and not auth.csrf_ok(principal.csrf_token or "", request.headers.get(auth.CSRF_HEADER)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")
    request.state.actor = principal.actor
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
