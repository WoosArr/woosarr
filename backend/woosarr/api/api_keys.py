"""API keys for scripted access, e.g. reading logs remotely (D16)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.api.schemas import Ok
from woosarr.core import auth
from woosarr.db.models import AdminUser, ApiKey, utcnow
from woosarr.services import audit

router = APIRouter(prefix="/api-keys", tags=["api-keys"], dependencies=[Depends(require_admin)])


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    scope: str
    owner: str
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool


class ApiKeyCreateIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scope: Literal["read", "admin"] = "read"


class ApiKeyCreated(ApiKeyOut):
    # Shown once. Only its hash is stored.
    key: str


def _out(key: ApiKey, owner: str) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        scope=key.scope,
        owner=owner,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked=key.revoked_at is not None,
    )


@router.get("", response_model=list[ApiKeyOut])
def list_keys(db: DbDep) -> list[ApiKeyOut]:
    rows = db.execute(
        select(ApiKey, AdminUser.username).join(AdminUser).order_by(ApiKey.created_at.desc())
    ).all()
    return [_out(key, owner) for key, owner in rows]


@router.post("", response_model=ApiKeyCreated, status_code=201)
def create_key(body: ApiKeyCreateIn, db: DbDep, principal: AdminDep) -> ApiKeyCreated:
    if principal.via == "api_key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API keys can't create API keys")
    user = db.get(AdminUser, principal.user.id)
    new = auth.create_api_key(db, user, body.name, body.scope)
    db.flush()
    audit.record(db, principal.actor, "api_key_created", "api_key", str(new.row.id),
                 name=body.name, scope=body.scope, prefix=new.row.prefix)
    return ApiKeyCreated(**_out(new.row, user.username).model_dump(), key=new.raw)


@router.delete("/{key_id}", response_model=Ok)
def revoke_key(key_id: int, db: DbDep, principal: AdminDep) -> Ok:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        audit.record(db, principal.actor, "api_key_revoked", "api_key", str(key_id), prefix=key.prefix)
    return Ok()
