"""Reaction posts: a pinned message whose reactions grant/remove libraries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.db.models import Library, Reaction, ReactionPost
from woosarr.services import audit
from woosarr.services.discord_bot import DiscordConfigError, normalize_emoji

router = APIRouter(prefix="/reaction-posts", tags=["reaction-posts"], dependencies=[Depends(require_admin)])


class ReactionOption(BaseModel):
    emoji: Annotated[str, Field(min_length=1, max_length=100)]
    label: Annotated[str, Field(max_length=100)] = ""
    library_ids: Annotated[list[str], Field(min_length=1, max_length=50)]

    @field_validator("emoji")
    @classmethod
    def _emoji(cls, v: str) -> str:
        return normalize_emoji(v)


class ReactionPostIn(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=100)]
    content: Annotated[str, Field(min_length=1, max_length=2000)]  # Discord message limit
    channel_id: Annotated[str | None, Field(pattern=r"^\d{5,25}$")] = None
    pin: bool = True
    enabled: bool = True
    options: Annotated[list[ReactionOption], Field(max_length=20)] = []  # Discord: 20 reactions per message


class ReactionPostOut(ReactionPostIn):
    id: int
    message_id: str | None
    published_at: datetime | None
    reactions: dict[str, int]


class PublishIn(BaseModel):
    # Create (or reuse) a text channel with this name instead of channel_id.
    new_channel_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None


class PublishOut(BaseModel):
    channel_id: str
    channel: str
    message_id: str
    pinned: bool
    jump_url: str


def _out(db, post: ReactionPost) -> ReactionPostOut:
    counts = dict(db.execute(select(Reaction.emoji, func.count()).where(Reaction.post_id == post.id)
                             .group_by(Reaction.emoji)).all())
    return ReactionPostOut(id=post.id, title=post.title, content=post.content, channel_id=post.channel_id,
                           pin=post.pin, enabled=post.enabled, options=[ReactionOption(**o) for o in post.options],
                           message_id=post.message_id, published_at=post.published_at, reactions=counts)


def _validate(db, body: ReactionPostIn) -> list[dict]:
    emojis = [o.emoji for o in body.options]
    if len(set(emojis)) != len(emojis):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "each emoji can only be used once")
    wanted = {lib for o in body.options for lib in o.library_ids}
    known = set(db.scalars(select(Library.library_id).where(Library.library_id.in_(wanted))))
    if wanted - known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown libraries: {sorted(wanted - known)}")
    return [o.model_dump() for o in body.options]


def _get(db, post_id: int) -> ReactionPost:
    post = db.get(ReactionPost, post_id)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reaction post not found")
    return post


@router.get("", response_model=list[ReactionPostOut])
def list_posts(db: DbDep) -> list[ReactionPostOut]:
    return [_out(db, p) for p in db.scalars(select(ReactionPost).order_by(ReactionPost.id))]


@router.post("", response_model=ReactionPostOut, status_code=201)
def create_post(body: ReactionPostIn, request: Request, db: DbDep, principal: AdminDep) -> ReactionPostOut:
    post = ReactionPost(title=body.title, content=body.content, channel_id=body.channel_id, pin=body.pin,
                        enabled=body.enabled, options=_validate(db, body))
    db.add(post)
    db.flush()
    audit.record(db, principal.actor, "reaction_post_created", "reaction_post", str(post.id), title=body.title)
    return _out(db, post)


@router.put("/{post_id}", response_model=ReactionPostOut)
def update_post(post_id: int, body: ReactionPostIn, request: Request, db: DbDep, principal: AdminDep) -> ReactionPostOut:
    post = _get(db, post_id)
    post.title, post.content, post.pin, post.enabled = body.title, body.content, body.pin, body.enabled
    if body.channel_id != post.channel_id:
        post.channel_id, post.message_id = body.channel_id, None  # moves to a new message on publish
    post.options = _validate(db, body)
    db.flush()
    audit.record(db, principal.actor, "reaction_post_updated", "reaction_post", str(post_id),
                 options=post.options, enabled=post.enabled)
    # Mapping or enabled changes can move access for everyone who reacted.
    request.app.state.sync.trigger(None, f"reaction post {post_id} changed")
    return _out(db, post)


@router.delete("/{post_id}", status_code=204)
async def delete_post(post_id: int, request: Request, principal: AdminDep) -> None:
    svc = request.app.state.discord
    db_ = request.app.state.db
    with db_.session() as s:
        post = _get(s, post_id)
        channel_id, message_id = post.channel_id, post.message_id
        s.delete(post)
        audit.record(s, principal.actor, "reaction_post_deleted", "reaction_post", str(post_id))
    if svc is not None:
        await svc.unpublish_post(channel_id, message_id)
    request.app.state.sync.trigger(None, f"reaction post {post_id} deleted")


@router.post("/{post_id}/publish", response_model=PublishOut)
async def publish(post_id: int, body: PublishIn, request: Request, principal: AdminDep) -> PublishOut:
    svc = request.app.state.discord
    if svc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Discord isn't available in demo mode")
    try:
        result = await svc.publish_post(post_id, body.new_channel_name)
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    request.app.state.sync.trigger(None, f"reaction post {post_id} published by {principal.actor}")
    return PublishOut(**result)
