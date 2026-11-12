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
from woosarr.api.users import RoleImpactOut
from woosarr.db.models import DiscordMember, DiscordRole, Reaction, ReactionPost
from woosarr.services import audit, impact, reaction_text
from woosarr.services.discord_bot import (
    DiscordConfigError,
    DiscordService,
    normalize_emoji,
    option_emojis,
    option_role_ids,
)

router = APIRouter(prefix="/reaction-posts", tags=["reaction-posts"], dependencies=[Depends(require_admin)])


class ReactionOption(BaseModel):
    emoji: Annotated[str, Field(min_length=1, max_length=100)]
    # One Discord role per reaction.
    role_id: Annotated[str, Field(pattern=r"^\d{1,25}$")]
    # Always the role's name; set by WooSarr on save (anything sent is ignored).
    label: Annotated[str, Field(max_length=100)] = ""
    # Optional legend line for this reaction instead of the post's template (same placeholders).
    text: Annotated[str, Field(max_length=300)] = ""
    # Emoji this reaction used before; members' reactions with them still count. Set by WooSarr
    # (anything sent is ignored).
    former: list[str] = []

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
    # The next publish removes reactions for emoji the post no longer uses.
    clear_old_reactions: bool


class ReactionPostUpdate(ReactionPostIn):
    # When a published post's emoji change: True = reactions with the old emoji keep giving the
    # role (the bot only takes its own old reaction off); False = they stop counting and the next
    # publish removes them from the message (needs Manage Messages).
    keep_old_reactions: bool = True


class ReactionChange(BaseModel):
    emoji: str  # the old emoji
    new_emoji: str | None  # what the same role uses now (None: the role's reaction was removed)
    role_id: str
    role_name: str
    members: int  # members who reacted with the old emoji
    names: list[str]  # the first few of them


class ReactionChangesOut(BaseModel):
    published: bool
    moved: bool  # another channel: a new message; nobody's reactions carry over
    changes: list[ReactionChange]
    # Members who'd lose a role, and the Plex impact, for each choice.
    keep_losing: int
    keep_impact: list[RoleImpactOut]
    clear_losing: int
    clear_impact: list[RoleImpactOut]


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
    notes: list[str] = []  # things that didn't work but didn't stop the publish


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
                           outdated=bool(post.message_id) and post.rendered_content != text,
                           clear_old_reactions=post.clear_old_reactions)


def _option_out(db, o: dict) -> ReactionOption:
    role_ids = sorted(option_role_ids(o))
    role_id = role_ids[0] if role_ids else ""
    role = db.get(DiscordRole, role_id) if role_id else None
    return ReactionOption.model_construct(emoji=o.get("emoji", ""), role_id=role_id,
                                          label=role.name if role else o.get("label", ""), text=o.get("text", ""),
                                          former=list(o.get("former") or []))


def _carry_former(old: list[dict], new: list[dict]) -> list[dict]:
    """Keep old reactions working: each new option remembers the emoji its role used before (and
    their own former ones) unless another option uses them now."""
    in_use = {normalize_emoji(n["emoji"]) for n in new}
    taken: set[str] = set()
    out = []
    for n in new:
        former = []
        for o in old:
            if option_role_ids(o) != {n["role_id"]}:
                continue
            for e in option_emojis(o):
                if e not in in_use and e not in taken:
                    former.append(e)
                    taken.add(e)
        out.append(dict(n, former=former) if former else n)
    return out


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
def update_post(post_id: int, body: ReactionPostUpdate, request: Request, db: DbDep,
                principal: AdminDep) -> ReactionPostOut:
    post = _get(db, post_id)
    options = _validate(db, body)
    # Old reactions only matter on the same message; a new channel means a new message.
    had_message = bool(post.message_id)
    same_message = had_message and body.channel_id == post.channel_id
    post.title, post.content, post.pin, post.enabled = body.title, body.content, body.pin, body.enabled
    post.legend, post.legend_template, post.legend_header = body.legend, body.legend_template, body.legend_header
    if body.channel_id != post.channel_id:
        post.channel_id, post.message_id = body.channel_id, None  # moves to a new message on publish
    old = post.options or []
    post.options = _carry_former(old, options) if same_message and body.keep_old_reactions else options
    # Emoji that stop counting: the next publish removes the roles members got from them.
    counted = {e for o in post.options for e in option_emojis(o)}
    retired = {normalize_emoji(r["emoji"]): r for r in post.retired_options or []}
    for o in old:
        for e in option_emojis(o):
            if e not in counted:
                retired.setdefault(e, {"emoji": e, "role_id": next(iter(sorted(option_role_ids(o))), "")})
    post.retired_options = [r for e, r in retired.items() if e not in counted] if had_message else []
    post.clear_old_reactions = same_message and not body.keep_old_reactions
    db.flush()
    audit.record(db, principal.actor, "reaction_post_updated", "reaction_post", str(post_id),
                 options=post.options, enabled=post.enabled,
                 **({"retired_emoji": [r["emoji"] for r in post.retired_options],
                     "old_reactions": "keep" if body.keep_old_reactions else "clear"} if post.retired_options else {}))
    return _out(db, post)


@router.post("/{post_id}/reaction-changes", response_model=ReactionChangesOut)
def reaction_changes(post_id: int, body: ReactionPostIn, db: DbDep) -> ReactionChangesOut:
    """What saving these options would do to members who already reacted (no Discord calls)."""
    post = _get(db, post_id)
    empty = ReactionChangesOut(published=bool(post.message_id), moved=False, changes=[], keep_losing=0,
                               keep_impact=[], clear_losing=0, clear_impact=[])
    if not post.message_id:
        return empty
    moved = body.channel_id != post.channel_id
    new = [{"emoji": normalize_emoji(o.emoji), "role_id": o.role_id} for o in body.options]
    in_use = {o["emoji"] for o in new}
    reacted: dict[str, list[str]] = {}
    for r in db.scalars(select(Reaction).where(Reaction.post_id == post.id)):
        reacted.setdefault(r.emoji, []).append(r.discord_user_id)
    names = {m.discord_user_id: m.display_name or m.username for m in db.scalars(
        select(DiscordMember).where(DiscordMember.discord_user_id.in_({u for us in reacted.values() for u in us})))}
    changes = []
    for o in [*(post.options or []), *(post.retired_options or [])]:
        role_id = next(iter(sorted(option_role_ids(o))), "")
        role = db.get(DiscordRole, role_id)
        for e in option_emojis(o):
            if (moved or e not in in_use) and reacted.get(e):
                users = sorted(reacted[e], key=lambda u: names.get(u, u).lower())
                changes.append(ReactionChange(
                    emoji=e, new_emoji=None if moved else next((n["emoji"] for n in new if n["role_id"] == role_id), None),
                    role_id=role_id, role_name=role.name if role else role_id, members=len(users),
                    names=[names.get(u, u) for u in users[:8]]))
    if not changes:
        return empty.model_copy(update={"moved": moved})
    before = DiscordService._reaction_roles(db, with_retired=True)[1]

    def outcome(options: list[dict]) -> tuple[int, list[RoleImpactOut]]:
        after = DiscordService._reaction_roles(db, {post.id: options})[1]
        losing: dict[frozenset[str], list[str]] = {}
        for uid, roles in before.items():
            lost = frozenset(roles - after.get(uid, set()))
            if lost:
                losing.setdefault(lost, []).append(uid)
        merged: dict[str, dict] = {}
        for lost, uids in losing.items():
            for row in impact.role_change_impact(db, uids, [], lost):
                m = merged.setdefault(row["plex_user_id"], {**row, "gain": set(), "lose": set()})
                m["gain"].update(row["gain"])
                m["lose"].update(row["lose"])
        return sum(len(u) for u in losing.values()), [
            RoleImpactOut(**{**m, "gain": sorted(m["gain"]), "lose": sorted(m["lose"])})
            for m in sorted(merged.values(), key=lambda m: m["username"].lower())]

    # Moved: the new message starts without reactions, whichever choice.
    clear = outcome([] if moved else new)
    keep = clear if moved else outcome(_carry_former(post.options or [], new))
    return ReactionChangesOut(published=True, moved=moved, changes=changes, keep_losing=keep[0],
                              keep_impact=keep[1], clear_losing=clear[0], clear_impact=clear[1])


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
