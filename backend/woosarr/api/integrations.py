"""Setup wizards: Plex OAuth + server, Discord token + server (P3-01, P3-02, P9-01, P9-02)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import app_settings
from woosarr.db.models import Library, RoleLibrary
from woosarr.plex_api_gateway import PlexAuthError
from woosarr.services import audit, discord_bot
from woosarr.services import discord_features as features
from woosarr.services.discord_bot import DiscordConfigError

log = logging.getLogger("woosarr.setup")
router = APIRouter(tags=["integrations"], dependencies=[Depends(require_admin)])


def _plex(request: Request):
    svc = request.app.state.plex
    if svc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Plex setup is disabled in demo mode")
    return svc


def _discord(request: Request):
    svc = request.app.state.discord
    if svc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Discord setup is disabled in demo mode")
    return svc


@contextmanager
def plex_errors() -> Iterator[None]:
    """Map Plex failures to HTTP errors; HTTPExceptions pass through untouched."""
    try:
        yield
    except HTTPException:
        raise
    except PlexAuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except Exception as exc:  # any Plex/network failure: 502 with the reason, traceback in the logs
        log.exception("plex call failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Plex error: {type(exc).__name__}: {exc}") from None


# ---- Plex ---------------------------------------------------------------------


class PlexStatus(BaseModel):
    configured: bool
    connected: bool
    signed_in: bool
    account: dict[str, Any] | None
    server: dict[str, Any] | None
    detail: str | None
    last_ok_at: float | None = None
    last_error: str | None = None


class PlexLoginStart(BaseModel):
    pin_id: str
    auth_url: str


class PlexLoginPollIn(BaseModel):
    pin_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class PlexLoginPoll(BaseModel):
    state: Literal["pending", "done", "expired"]
    account: dict[str, Any] | None = None


class PlexServerOut(BaseModel):
    machine_id: str
    name: str
    online: bool
    version: str | None
    platform: str | None


class PlexServerIn(BaseModel):
    machine_id: Annotated[str, Field(min_length=8, max_length=128)]


class WriteCheckIn(BaseModel):
    plex_user_id: str
    confirm: Literal[True]


DEMO_PLEX = {"configured": True, "connected": True, "signed_in": True, "account": {"username": "demo"},
             "server": {"name": "Demo server", "machine_id": "demo"}, "detail": "Demo mode: fake Plex data"}
DEMO_DISCORD = {"configured": True, "connected": True, "bot": {"username": "demo-bot"},
                "guild": {"id": "1", "name": "Demo server"}, "invite_url": None, "detail": "Demo mode: fake Discord data",
                "features": [dict(f, available=False, notes=["demo mode"]) for f in features.feature_report({})]}


@router.get("/plex/status", response_model=PlexStatus)
def plex_status(request: Request) -> PlexStatus:
    svc = request.app.state.plex
    return PlexStatus(**(svc.status() if svc else DEMO_PLEX))


@router.post("/plex/login", response_model=PlexLoginStart)
def plex_login_start(request: Request) -> PlexLoginStart:
    # Plex sends the popup back here when sign-in finishes.
    forward = str(request.base_url).rstrip("/") + "/setup?plex=signed-in"
    with plex_errors():
        pin_id, url = _plex(request).start_login(forward)
    return PlexLoginStart(pin_id=pin_id, auth_url=url)


@router.post("/plex/login/poll", response_model=PlexLoginPoll)
def plex_login_poll(body: PlexLoginPollIn, request: Request, db: DbDep, principal: AdminDep) -> PlexLoginPoll:
    svc = _plex(request)
    with plex_errors():
        state, token = svc.poll_token(body.pin_id)
        if state != "done" or token is None:
            return PlexLoginPoll(state=state)
        account = svc.complete_owner_login(token)
    audit.record(db, principal.actor, "plex_signed_in", "plex_account", account["id"], username=account["username"])
    return PlexLoginPoll(state="done", account=account)


@router.get("/plex/servers", response_model=list[PlexServerOut])
def plex_servers(request: Request) -> list[PlexServerOut]:
    with plex_errors():
        return [PlexServerOut(**s) for s in _plex(request).list_servers()]


@router.get("/plex/server", response_model=PlexServerOut | None)
def plex_server_info(request: Request) -> PlexServerOut | None:
    """The selected server, read live from plex.tv (name, version, online)."""
    svc = request.app.state.plex
    if svc is None:
        return PlexServerOut(machine_id="demo", name="Demo server", online=True, version="demo", platform="demo")
    current = svc.server()
    if not current:
        return None
    with plex_errors():
        live = next((s for s in svc.list_servers() if s["machine_id"] == current["machine_id"]), None)
    if live is None:
        return PlexServerOut(machine_id=current["machine_id"], name=current["name"], online=False,
                             version=None, platform=None)
    return PlexServerOut(**live)


@router.put("/plex/server", response_model=PlexStatus)
def plex_select_server(body: PlexServerIn, request: Request, db: DbDep, principal: AdminDep) -> PlexStatus:
    svc = _plex(request)
    with plex_errors():
        server = svc.select_server(body.machine_id)
    audit.record(db, principal.actor, "plex_server_selected", "plex_server", server["machine_id"], name=server["name"])
    request.app.state.sync.trigger(None, "plex server selected", delay=1)
    return PlexStatus(**svc.status())


@router.delete("/plex", response_model=PlexStatus)
def plex_disconnect(request: Request, db: DbDep, principal: AdminDep) -> PlexStatus:
    svc = _plex(request)
    svc.disconnect()
    audit.record(db, principal.actor, "plex_disconnected", "plex_account", None)
    return PlexStatus(**svc.status())


@router.post("/plex/check/read")
def plex_read_check(request: Request) -> dict[str, Any]:
    """Read-only: libraries, users and every user's shares. Nothing is written."""
    with plex_errors():
        return _plex(request).read_check()


@router.post("/plex/check/write")
def plex_write_check(body: WriteCheckIn, request: Request, db: DbDep, principal: AdminDep) -> dict[str, Any]:
    """Changes one test user's shares and restores them, to verify Plex's share behaviour (P0-03)."""
    with plex_errors():
        result = _plex(request).write_check(body.plex_user_id)
    audit.record(db, principal.actor, "plex_write_check", "plex_user", body.plex_user_id, passed=result["passed"],
                 steps=result["steps"])
    return result


# ---- Discord ------------------------------------------------------------------


class FeatureOut(BaseModel):
    key: str
    label: str
    description: str
    permissions: list[str]
    available: bool
    missing: list[str]
    notes: list[str]


class DiscordStatus(BaseModel):
    configured: bool
    connected: bool
    bot: dict[str, Any] | None
    guild: dict[str, Any] | None
    invite_url: str | None
    detail: str | None
    features: list[FeatureOut] = []


class DiscordTokenIn(BaseModel):
    token: Annotated[str, Field(min_length=20, max_length=200)]


class DiscordGuildIn(BaseModel):
    guild_id: Annotated[str, Field(pattern=r"^\d{5,25}$")]


class GuildOut(BaseModel):
    id: str
    name: str


def _discord_status(svc) -> DiscordStatus:
    return DiscordStatus(**svc.status(), features=svc.feature_status())


@router.get("/discord/status", response_model=DiscordStatus)
def discord_status(request: Request) -> DiscordStatus:
    svc = request.app.state.discord
    return _discord_status(svc) if svc else DiscordStatus(**DEMO_DISCORD)


class InviteUrlOut(BaseModel):
    url: str | None
    permissions: int


@router.get("/discord/invite-url", response_model=InviteUrlOut)
def discord_invite_url(request: Request, features_csv: Annotated[str, Query(alias="features")] = "") -> InviteUrlOut:
    """Bot invite link with exactly the permissions the chosen features need."""
    keys = {k for k in features_csv.split(",") if k}
    unknown = keys - features.FEATURE_KEYS
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown features: {sorted(unknown)}")
    value = features.permissions_value(keys)
    svc = request.app.state.discord
    bot = svc.config()[0] if svc else None
    url = (discord_bot.invite_url(bot["application_id"]).replace("permissions=0", f"permissions={value}")
           if bot else None)
    return InviteUrlOut(url=url, permissions=value)


class RoleCreateIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    # plain/reaction: a new role, optionally mapped to library_ids (Roles page, reaction posts)
    purpose: Literal["verified_role_id", "unverified_role_id", "linked_role_id", "library", "reaction", "plain"]
    library_id: str | None = None
    library_ids: Annotated[list[str], Field(max_length=200)] = []
    color: Annotated[int | None, Field(ge=0, le=0xFFFFFF)] = None


class RoleCreated(BaseModel):
    role_id: str
    name: str


@router.post("/discord/roles", response_model=RoleCreated, status_code=201)
async def discord_create_role(body: RoleCreateIn, request: Request, principal: AdminDep) -> RoleCreated:
    """Create a Discord role and use it as a link-status role or for one library."""
    svc = _discord(request)
    libraries = set(body.library_ids) | ({body.library_id} if body.purpose == "library" and body.library_id else set())
    if body.purpose == "library" and not body.library_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library not found")
    known = await svc.run_db(lambda s: set(s.scalars(select(Library.library_id)
                                                    .where(Library.library_id.in_(libraries)))))
    if libraries - known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown libraries: {sorted(libraries - known)}")
    try:
        role_id, name = await svc.create_role(body.name, body.color)
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    def save(s):
        for lib in sorted(libraries):
            s.add(RoleLibrary(role_id=role_id, library_id=lib))
        if body.purpose.endswith("_role_id"):
            app_settings.update(s, {body.purpose: role_id})
        audit.record(s, principal.actor, "discord_role_created", "role", role_id, name=name, purpose=body.purpose,
                     library_ids=sorted(libraries))

    await svc.run_db(save)
    if libraries:
        request.app.state.sync.trigger(None, f"role {name} created with libraries")
    if body.purpose.endswith("_role_id"):
        svc.schedule_role_sync()
    return RoleCreated(role_id=role_id, name=name)


class EmojiOut(BaseModel):
    id: str
    name: str
    animated: bool
    text: str  # <:name:id> / <a:name:id>, what reaction options store
    url: str
    available: bool


@router.get("/discord/emojis", response_model=list[EmojiOut])
async def discord_emojis(request: Request) -> list[EmojiOut]:
    svc = request.app.state.discord
    if svc is None:
        return []  # demo mode
    try:
        return [EmojiOut(**e) for e in await svc.list_emojis()]
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


class ChannelOut(BaseModel):
    id: str
    name: str
    category: str
    can_post: bool  # reaction posts
    can_post_requests: bool = False  # public requests


@router.get("/discord/channels", response_model=list[ChannelOut])
async def discord_channels(request: Request) -> list[ChannelOut]:
    svc = request.app.state.discord
    if svc is None:
        return []  # demo mode: no real channels
    try:
        return [ChannelOut(**c) for c in await svc.list_channels()]
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.put("/discord/token", response_model=DiscordStatus)
async def discord_set_token(body: DiscordTokenIn, request: Request, principal: AdminDep) -> DiscordStatus:
    svc = _discord(request)
    try:
        info = await svc.set_token(body.token)
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    await svc.run_db(lambda s: audit.record(s, principal.actor, "discord_token_set", "discord_bot", info.bot_user_id,
                                            bot=info.username))
    return _discord_status(svc)


@router.get("/discord/guilds", response_model=list[GuildOut])
async def discord_guilds(request: Request) -> list[GuildOut]:
    try:
        return [GuildOut(**g) for g in await _discord(request).guilds()]
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.put("/discord/guild", response_model=DiscordStatus)
async def discord_set_guild(body: DiscordGuildIn, request: Request, principal: AdminDep) -> DiscordStatus:
    svc = _discord(request)
    try:
        guild = await svc.set_guild(body.guild_id)
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    await svc.run_db(lambda s: audit.record(s, principal.actor, "discord_guild_selected", "discord_guild", guild["id"],
                                            name=guild["name"]))
    return _discord_status(svc)


@router.post("/discord/restart", response_model=DiscordStatus)
async def discord_restart(request: Request) -> DiscordStatus:
    svc = _discord(request)
    await svc.restart()
    return _discord_status(svc)


@router.delete("/discord", response_model=DiscordStatus)
async def discord_disconnect(request: Request, principal: AdminDep) -> DiscordStatus:
    svc = _discord(request)
    await svc.disconnect()
    await svc.run_db(lambda s: audit.record(s, principal.actor, "discord_disconnected", "discord_bot", None))
    return _discord_status(svc)
