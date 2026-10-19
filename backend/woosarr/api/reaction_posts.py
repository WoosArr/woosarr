"""Reaction posts: a pinned message whose reactions give/remove Discord roles.

Libraries follow from the normal role -> library mappings (Roles page).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.db.models import DiscordRole, Reaction, ReactionPost
from woosarr.services import audit, reaction_text
from woosarr.services.discord_bot import DiscordConfigError, normalize_emoji, option_role_ids

router = APIRouter(prefix="/reaction-posts", tags=["reaction-posts"], dependencies=[Depends(require_admin)])


class ReactionOption(BaseModel):
    emoji: Annotated[str, Field(min_length=1, max_length=100)]
    # One Discord role per reaction.
    role_id: Annotated[str, Field(pattern=r"^\d{1,25}$")]
    # Always the role's name; set by WooSarr on save (anything sent is ignored).
    label: Annotated[str, Field(max_length=100)] = ""
    # Optional legend line for this reaction instead of the post's template (same placeholders).
    text: Annotated[str, Field(max_length=300)] = ""

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
    # Legend: one line per reaction under the message (placeholders: {emoji} {role} {role_name} {libraries}).
    legend: bool = True
    legend_template: Annotated[str, Field(min_length=1, max_length=300)] = reaction_text.DEFAULT_LINE
    legend_header: Annotated[str, Field(max_length=200)] = ""


class ReactionPostOut(ReactionPostIn):
    id: int
    message_id: str | None
    published_at: datetime | None
    reactions: dict[str, int]
    # The full message as it is (or would be) posted.
    preview: str
    preview_length: int
    # The posted message differs from the current text (an update is pending).
    outdated: bool


class PreviewOut(BaseModel):
    text: str
    length: int
    max_length: int = reaction_text.MAX_MESSAGE


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
    text = reaction_text.render_post(db, post)
    return ReactionPostOut(id=post.id, title=post.title, content=post.content, channel_id=post.channel_id,
                           pin=post.pin, enabled=post.enabled, legend=post.legend,
                           legend_template=post.legend_template, legend_header=post.legend_header,
                           options=[_option_out(db, o) for o in post.options],
                           message_id=post.message_id, published_at=post.published_at, reactions=counts,
                           preview=text, preview_length=len(text),
                           outdated=bool(post.message_id) and post.rendered_content != text)


def _option_out(db, o: dict) -> ReactionOption:
    role_ids = sorted(option_role_ids(o))
    role_id = role_ids[0] if role_ids else ""
    role = db.get(DiscordRole, role_id) if role_id else None
    return ReactionOption.model_construct(emoji=o.get("emoji", ""), role_id=role_id,
                                          label=role.name if role else o.get("label", ""), text=o.get("text", ""))


def _validate(db, body: ReactionPostIn) -> list[dict]:
    emojis = [o.emoji for o in body.options]
    if len(set(emojis)) != len(emojis):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "each emoji can only be used once")
    wanted = {o.role_id for o in body.options}
    roles = {r.role_id: r for r in db.scalars(select(DiscordRole).where(DiscordRole.role_id.in_(wanted)))}
    unknown = sorted(wanted - set(roles))
    if unknown:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown Discord roles: {unknown}")
    unusable = sorted((r.name for r in roles.values() if r.managed or r.deleted), key=str.lower)
    if unusable:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"these roles can't be given by a bot: {unusable}")
    # The label is the role's name.
    options = [{"emoji": o.emoji, "role_id": o.role_id, "label": roles[o.role_id].name, "text": o.text.strip()}
               for o in body.options]
    text = reaction_text.render_post(db, _Draft(body, options))
    if len(text) > reaction_text.MAX_MESSAGE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"the message would be {len(text)} characters; Discord allows {reaction_text.MAX_MESSAGE}")
    return options


class _Draft:
    """A not-yet-saved post, shaped like ReactionPost for rendering."""

    def __init__(self, body: ReactionPostIn, options: list[dict]) -> None:
        self.content, self.options, self.legend = body.content, options, body.legend
        self.legend_template, self.legend_header = body.legend_template, body.legend_header


def _get(db, post_id: int) -> ReactionPost:
    post = db.get(ReactionPost, post_id)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reaction post not found")
    return post


@router.post("/preview", response_model=PreviewOut)
def preview_post(body: ReactionPostIn, db: DbDep) -> PreviewOut:
    """The message text a draft would post (legend included), without saving."""
    options = [{"emoji": o.emoji, "role_id": o.role_id, "text": o.text} for o in body.options]
    text = reaction_text.render_post(db, _Draft(body, options))
    return PreviewOut(text=text, length=len(text))


@router.get("", response_model=list[ReactionPostOut])
def list_posts(db: DbDep) -> list[ReactionPostOut]:
    return [_out(db, p) for p in db.scalars(select(ReactionPost).order_by(ReactionPost.id))]


@router.post("", response_model=ReactionPostOut, status_code=201)
def create_post(body: ReactionPostIn, request: Request, db: DbDep, principal: AdminDep) -> ReactionPostOut:
    post = ReactionPost(title=body.title, content=body.content, channel_id=body.channel_id, pin=body.pin,
                        enabled=body.enabled, options=_validate(db, body), legend=body.legend,
                        legend_template=body.legend_template, legend_header=body.legend_header)
    db.add(post)
    db.flush()
    audit.record(db, principal.actor, "reaction_post_created", "reaction_post", str(post.id), title=body.title)
    return _out(db, post)


@router.put("/{post_id}", response_model=ReactionPostOut)
def update_post(post_id: int, body: ReactionPostIn, request: Request, db: DbDep, principal: AdminDep) -> ReactionPostOut:
    post = _get(db, post_id)
    post.title, post.content, post.pin, post.enabled = body.title, body.content, body.pin, body.enabled
    post.legend, post.legend_template, post.legend_header = body.legend, body.legend_template, body.legend_header
    if body.channel_id != post.channel_id:
        post.channel_id, post.message_id = body.channel_id, None  # moves to a new message on publish
    post.options = _validate(db, body)
    db.flush()
    audit.record(db, principal.actor, "reaction_post_updated", "reaction_post", str(post_id),
                 options=post.options, enabled=post.enabled)
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
        # Roles members got from this post are left as they are.
        await svc.unpublish_post(channel_id, message_id)


@router.post("/{post_id}/publish", response_model=PublishOut)
async def publish(post_id: int, body: PublishIn, request: Request, principal: AdminDep) -> PublishOut:
    svc = request.app.state.discord
    if svc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Discord isn't available in demo mode")
    try:
        result = await svc.publish_post(post_id, body.new_channel_name)  # also re-syncs reaction roles
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return PublishOut(**result)
