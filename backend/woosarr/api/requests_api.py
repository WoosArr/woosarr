"""Requests through Seerr: connection setup and the request list (P13-01)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.services import audit, media_requests
from woosarr.services.seerr import SeerrError

router = APIRouter(tags=["requests"], dependencies=[Depends(require_admin)])


class SeerrStatus(BaseModel):
    configured: bool
    connected: bool
    url: str | None
    version: str | None
    admin: str | None
    detail: str | None
    last_ok_at: float | None = None
    command_registered: str | None = None  # name the bot registered, None if not (yet)


class SeerrIn(BaseModel):
    url: Annotated[str, Field(min_length=8, max_length=300)]
    # Omit to keep the stored key (changing only the address).
    api_key: Annotated[str | None, Field(min_length=16, max_length=200)] = None


class SeerrTest(BaseModel):
    url: str
    version: str
    admin: str
    users: int
    plex_users: int


class RequestOut(BaseModel):
    id: int
    seerr_request_id: int
    media_type: Literal["movie", "tv"]
    tmdb_id: int
    title: str
    year: str
    seasons: list[int]
    status: str
    discord_user_id: str
    discord_name: str | None
    plex_username: str | None
    created_at: datetime
    updated_at: datetime
    checked_at: datetime | None


class PollOut(BaseModel):
    notified: int


def _svc(request: Request) -> media_requests.RequestService:
    return request.app.state.requests


def _status(request: Request) -> SeerrStatus:
    discord = request.app.state.discord
    client = getattr(discord, "_client", None) if discord else None
    return SeerrStatus(**_svc(request).status(), command_registered=getattr(client, "request_name", None))


def _after_change(request: Request) -> None:
    if request.app.state.discord is not None:
        request.app.state.discord.schedule_command_sync()


@router.get("/requests/seerr", response_model=SeerrStatus)
def seerr_status(request: Request) -> SeerrStatus:
    return _status(request)


@router.post("/requests/seerr/test", response_model=SeerrTest)
def seerr_test(body: SeerrIn, request: Request) -> SeerrTest:
    """Check the address and key (or the stored key) without saving anything."""
    try:
        return SeerrTest(**_svc(request).test(body.url, body.api_key))
    except SeerrError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.put("/requests/seerr", response_model=SeerrStatus)
def seerr_save(body: SeerrIn, request: Request, db: DbDep, principal: AdminDep) -> SeerrStatus:
    try:
        info = _svc(request).save(body.url, body.api_key)
    except SeerrError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    audit.record(db, principal.actor, "seerr_connected", "seerr", info["url"], version=info["version"],
                 key_replaced=body.api_key is not None)
    _after_change(request)
    return _status(request)


@router.delete("/requests/seerr", response_model=SeerrStatus)
def seerr_disconnect(request: Request, db: DbDep, principal: AdminDep) -> SeerrStatus:
    _svc(request).disconnect()
    audit.record(db, principal.actor, "seerr_disconnected", "seerr", None)
    _after_change(request)
    return _status(request)


@router.get("/requests", response_model=list[RequestOut])
def list_requests(db: DbDep, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[RequestOut]:
    return [RequestOut(**r) for r in media_requests.recent(db, limit)]


@router.post("/requests/poll", response_model=PollOut)
async def poll_now(request: Request) -> PollOut:
    """Re-read open requests from Seerr now and DM members about changes."""
    notes = await asyncio.to_thread(_svc(request).poll)
    discord = request.app.state.discord
    sent = 0
    for uid, text in notes:
        if discord is not None and await discord.send_dm(uid, text):
            sent += 1
    return PollOut(notified=sent)
