"""Custom Emoji page: search emoji.gg / OpenMoji / slackmojis, add to the Discord server, rename and
delete the server's emoji. Previews are fetched by WooSarr (services/emoji_library.py)."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from woosarr.api.deps import AdminDep, require_admin
from woosarr.api.integrations import EmojiOut
from woosarr.db.models import Reaction, ReactionPost
from woosarr.services import audit
from woosarr.services.discord_bot import DiscordConfigError, normalize_emoji
from woosarr.services.emoji_library import LABELS, EmojiLibraryError, as_dict, prepare_image

log = logging.getLogger("woosarr.emoji")

router = APIRouter(tags=["emoji"], dependencies=[Depends(require_admin)])

Source = Literal["emojigg", "openmoji", "slackmojis"]
EmojiName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{2,32}$")]
ItemId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ServerEmojiId = Annotated[str, Path(pattern=r"^\d{1,25}$")]


class EmojiSourceOut(BaseModel):
    key: Source
    label: str
    license: str
    live: bool  # searched on the source's site (slackmojis); the others are downloaded indexes
    count: int | None
    updated_at: float | None
    checked_at: float | None
    version: str | None
    error: str | None


class LibraryEmojiOut(BaseModel):
    source: Source
    id: str
    name: str
    title: str
    category: str
    animated: bool | None
    page: str
    credit: str


class EmojiSearchOut(BaseModel):
    items: list[LibraryEmojiOut]
    total: int
    more: bool


class EmojiSlotsOut(BaseModel):
    limit: int
    static: int
    animated: int


class EmojiAddIn(BaseModel):
    source: Source
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    name: EmojiName


class EmojiRenameIn(BaseModel):
    name: EmojiName


def _library(request: Request):
    return request.app.state.emoji_library


def _guild(request: Request):
    return request.app.state.emoji_guild


def _in_session(request: Request, fn):
    with request.app.state.db.session() as s:
        return fn(s)


@router.get("/emoji/sources", response_model=list[EmojiSourceOut])
def sources(request: Request) -> list[EmojiSourceOut]:
    return [EmojiSourceOut(**s) for s in _library(request).sources()]


@router.get("/emoji/search", response_model=EmojiSearchOut)
async def search(request: Request, source: Source, q: Annotated[str, Query(max_length=100)] = "",
                 hide_animated: bool = True, offset: Annotated[int, Query(ge=0, le=10000)] = 0,
                 limit: Annotated[int, Query(ge=1, le=120)] = 60) -> EmojiSearchOut:
    try:
        items, total, more = await asyncio.to_thread(_library(request).search, source, q, hide_animated, offset, limit)
    except EmojiLibraryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    return EmojiSearchOut(items=[LibraryEmojiOut(**as_dict(e)) for e in items], total=total, more=more)


@router.get("/emoji/preview/{source}/{item_id}", response_class=Response,
            responses={200: {"content": {"image/png": {}, "image/gif": {}}}})
async def preview(request: Request, source: Source, item_id: ItemId) -> Response:
    lib = _library(request)
    try:
        data, kind = await asyncio.to_thread(lambda: lib.preview_bytes(lib.item(source, item_id)))
    except EmojiLibraryError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    # Overrides the API's no-store: the same preview is shown again and again while browsing.
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/discord/emojis/slots", response_model=EmojiSlotsOut)
def slots(request: Request) -> EmojiSlotsOut:
    try:
        return EmojiSlotsOut(**_guild(request).emoji_slots())
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("/emoji/add", response_model=EmojiOut)
async def add(request: Request, body: EmojiAddIn, principal: AdminDep) -> EmojiOut:
    lib = _library(request)
    try:
        item = lib.item(body.source, body.id)
        image, animated = await asyncio.to_thread(lambda: prepare_image(lib.image_bytes(item)))
    except EmojiLibraryError as exc:
        log.warning("emoji add failed source=%s id=%s: %s", body.source, body.id, exc)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    try:
        emoji = await _guild(request).add_emoji(body.name, image, animated,
                                                f"Added from {LABELS[body.source]} by {principal.actor} (WooSarr)")
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await asyncio.to_thread(_in_session, request, lambda s: audit.record(
        s, principal.actor, "discord_emoji_added", "emoji", str(emoji["id"]), name=emoji["name"], animated=animated,
        source=body.source, source_id=item.id, page=item.page, credit=item.credit))
    return EmojiOut(**emoji)


def _posts_using(s, emoji_id: str) -> list[str]:
    return [p.title for p in s.scalars(select(ReactionPost))
            if any(normalize_emoji(o.get("emoji", "")).endswith(f":{emoji_id}>") for o in p.options or [])]


@router.patch("/discord/emojis/{emoji_id}", response_model=EmojiOut)
async def rename(request: Request, emoji_id: ServerEmojiId, body: EmojiRenameIn, principal: AdminDep) -> EmojiOut:
    try:
        emoji, old = await _guild(request).rename_emoji(emoji_id, body.name, f"Renamed by {principal.actor} (WooSarr)")
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    new = str(emoji["text"])

    # Reaction posts store <:name:id> and reactions are matched on it: follow the new name.
    def follow(s) -> list[str]:
        changed = []
        for post in s.scalars(select(ReactionPost)):
            opts = [dict(o, emoji=new if normalize_emoji(o.get("emoji", "")) == old else o.get("emoji", ""),
                         **({"former": [new if normalize_emoji(e) == old else e for e in o["former"]]}
                            if o.get("former") else {}))
                    for o in post.options or []]
            if opts != post.options:
                post.options = opts
                changed.append(post.title)
        s.execute(update(Reaction).where(Reaction.emoji == old).values(emoji=new))
        audit.record(s, principal.actor, "discord_emoji_renamed", "emoji", emoji_id, old=old, new=new,
                     reaction_posts=changed)
        return changed

    with_posts = await asyncio.to_thread(_in_session, request, follow)
    if with_posts:
        log.info("emoji rename followed in reaction posts=%s", with_posts)
    return EmojiOut(**emoji)


@router.delete("/discord/emojis/{emoji_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(request: Request, emoji_id: ServerEmojiId, principal: AdminDep) -> None:
    used = await asyncio.to_thread(_in_session, request, lambda s: _posts_using(s, emoji_id))
    if used:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Reaction posts use this emoji ({', '.join(used)}); remove it from them first.")
    try:
        gone = await _guild(request).delete_emoji(emoji_id, f"Deleted by {principal.actor} (WooSarr)")
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    await asyncio.to_thread(_in_session, request, lambda s: audit.record(
        s, principal.actor, "discord_emoji_deleted", "emoji", emoji_id, name=gone["name"]))
