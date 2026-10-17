"""Users page API: Plex users, links, roles, access sources, manual grants (P7-01, P4-06)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from woosarr.api.deps import AdminDep, DbDep, require_admin
from woosarr.core import app_settings
from woosarr.db.models import (
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    ManualGrant,
    PlexInvite,
    PlexUser,
    utcnow,
)
from woosarr.services import audit
from woosarr.services.access import library_sources, load_snapshot, user_state

router = APIRouter(tags=["users"], dependencies=[Depends(require_admin)])


class LinkOut(BaseModel):
    discord_user_id: str
    discord_username: str | None
    discord_display_name: str | None
    in_guild: bool
    method: str
    verified: bool
    linked_at: datetime
    linked_by: str


class RoleRef(BaseModel):
    role_id: str
    name: str
    color: int


class LibraryAccess(BaseModel):
    library_id: str
    title: str
    sources: list[Literal["role", "manual", "reaction", "exempt", "external"]]
    shared: bool  # currently shared on Plex (last observed)


class UserOut(BaseModel):
    plex_user_id: str
    username: str
    email: str | None
    is_managed: bool
    is_owner: bool
    removed: bool
    link: LinkOut | None
    roles: list[RoleRef]
    libraries: list[LibraryAccess]
    manual_library_ids: list[str]


def _users(db, only: str | None = None) -> list[UserOut]:
    settings = app_settings.load(db)
    snap = load_snapshot(db)
    roles = {r.role_id: r for r in db.scalars(select(DiscordRole))}
    titles = dict(db.execute(select(Library.library_id, Library.title)).all())
    stmt = select(PlexUser).order_by(PlexUser.username)
    if only:
        stmt = stmt.where(PlexUser.plex_user_id == only)
    out = []
    for user in db.scalars(stmt):
        current = snap.cached_shares.get(user.plex_user_id, frozenset())
        link = snap.links.get(user.plex_user_id)
        member = snap.members.get(link.discord_user_id) if link else None
        state = user_state(snap, user.plex_user_id, settings, current)
        sources = library_sources(snap, state, current)
        out.append(
            UserOut(
                plex_user_id=user.plex_user_id,
                username=user.username,
                email=user.email,
                is_managed=user.is_managed,
                is_owner=user.is_owner,
                removed=user.removed,
                link=LinkOut(
                    discord_user_id=link.discord_user_id,
                    discord_username=member.username if member else None,
                    discord_display_name=member.display_name if member else None,
                    in_guild=bool(member and member.in_guild),
                    method=link.method,
                    verified=link.verified,
                    linked_at=link.linked_at,
                    linked_by=link.linked_by,
                )
                if link
                else None,
                roles=[
                    RoleRef(role_id=r, name=roles[r].name, color=roles[r].color)
                    for r in (member.role_ids if member and member.in_guild else [])
                    if r in roles and not roles[r].deleted
                ],
                libraries=[
                    LibraryAccess(library_id=lib, title=titles.get(lib, lib), sources=src, shared=lib in current)
                    for lib, src in sources.items()
                ],
                manual_library_ids=sorted(snap.manual.get(user.plex_user_id, frozenset())),
            )
        )
    return out


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: DbDep,
    link: Annotated[Literal["verified", "unverified", "unlinked"] | None, Query()] = None,
    include_removed: bool = False,
) -> list[UserOut]:
    users = _users(db)
    if not include_removed:
        users = [u for u in users if not u.removed]
    if link == "verified":
        users = [u for u in users if u.link and u.link.verified]
    elif link == "unverified":
        users = [u for u in users if u.link and not u.link.verified]
    elif link == "unlinked":
        users = [u for u in users if u.link is None]
    return users


@router.get("/users/{plex_user_id}", response_model=UserOut)
def get_user(plex_user_id: str, db: DbDep) -> UserOut:
    found = _users(db, plex_user_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plex user not found")
    return found[0]


class ManualGrantsIn(BaseModel):
    plex_user_ids: list[str] = Field(min_length=1, max_length=500)
    library_ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["add", "remove"]


class ManualGrantsOut(BaseModel):
    changed: int


@router.post("/users/manual-grants", response_model=ManualGrantsOut)
def manual_grants(body: ManualGrantsIn, request: Request, db: DbDep, principal: AdminDep) -> ManualGrantsOut:
    known_users = set(db.scalars(select(PlexUser.plex_user_id).where(PlexUser.plex_user_id.in_(body.plex_user_ids))))
    known_libs = set(db.scalars(select(Library.library_id).where(Library.library_id.in_(body.library_ids))))
    missing = (set(body.plex_user_ids) - known_users) | (set(body.library_ids) - known_libs)
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown ids: {sorted(missing)[:10]}")
    owners = set(db.scalars(select(PlexUser.plex_user_id).where(
        PlexUser.plex_user_id.in_(known_users), PlexUser.is_owner.is_(True))))
    if owners:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the server owner always has every library")
    existing = set(
        db.execute(
            select(ManualGrant.plex_user_id, ManualGrant.library_id).where(
                ManualGrant.plex_user_id.in_(known_users), ManualGrant.library_id.in_(known_libs)
            )
        ).all()
    )
    changed = 0
    for uid in sorted(known_users):
        for lib in sorted(known_libs):
            if body.action == "add" and (uid, lib) not in existing:
                db.add(ManualGrant(plex_user_id=uid, library_id=lib, granted_by=principal.actor))
                changed += 1
            elif body.action == "remove" and (uid, lib) in existing:
                db.execute(delete(ManualGrant).where(ManualGrant.plex_user_id == uid, ManualGrant.library_id == lib))
                changed += 1
    if changed:
        audit.record(db, principal.actor, f"manual_grant_{body.action}", "plex_users", ",".join(sorted(known_users))[:100],
                     plex_user_ids=sorted(known_users), library_ids=sorted(known_libs), changed=changed)
        request.app.state.sync.trigger(sorted(known_users), f"manual grants changed by {principal.actor}")
    return ManualGrantsOut(changed=changed)


class AdminLinkIn(BaseModel):
    discord_user_id: Annotated[str, Field(pattern=r"^\d{5,25}$")]


@router.put("/users/{plex_user_id}/link", response_model=UserOut)
def admin_link(plex_user_id: str, body: AdminLinkIn, request: Request, db: DbDep, principal: AdminDep) -> UserOut:
    if db.get(PlexUser, plex_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plex user not found")
    by_discord = db.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == body.discord_user_id))
    if by_discord and by_discord.plex_user_id != plex_user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "that Discord account is linked to another Plex user; unlink it first")
    link = db.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == plex_user_id))
    previous = link.discord_user_id if link else None
    if link is None:
        link = DiscordLink(plex_user_id=plex_user_id, discord_user_id=body.discord_user_id,
                           method="admin", verified=True, linked_by=principal.actor)
        db.add(link)
    else:
        link.discord_user_id, link.method, link.verified, link.linked_by = body.discord_user_id, "admin", True, principal.actor
    db.flush()
    audit.record(db, principal.actor, "link_admin", "plex_user", plex_user_id,
                 discord_user_id=body.discord_user_id, previous_discord_user_id=previous)
    request.app.state.sync.trigger([plex_user_id], f"linked by {principal.actor}")
    return _users(db, plex_user_id)[0]


@router.delete("/users/{plex_user_id}/link", response_model=UserOut)
def admin_unlink(plex_user_id: str, request: Request, db: DbDep, principal: AdminDep) -> UserOut:
    link = db.scalar(select(DiscordLink).where(DiscordLink.plex_user_id == plex_user_id))
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user is not linked")
    discord_id = link.discord_user_id
    db.delete(link)
    db.flush()
    audit.record(db, principal.actor, "unlink_admin", "plex_user", plex_user_id, discord_user_id=discord_id)
    # Unlinked users are no longer managed (N5); the reconcile refreshes their observed state.
    request.app.state.sync.trigger([plex_user_id], f"unlinked by {principal.actor}")
    return _users(db, plex_user_id)[0]


class InviteOut(BaseModel):
    id: int
    identifier: str
    discord_user_id: str
    discord_username: str | None
    invited_by: str
    verified: bool
    library_ids: list[str]
    status: str
    plex_user_id: str | None
    created_at: datetime
    resolved_at: datetime | None


@router.get("/invites", response_model=list[InviteOut])
def list_invites(db: DbDep, status_filter: Annotated[str | None, Query(alias="status")] = None) -> list[InviteOut]:
    names = dict(db.execute(select(DiscordMember.discord_user_id, DiscordMember.username)).all())
    stmt = select(PlexInvite).order_by(PlexInvite.created_at.desc()).limit(200)
    if status_filter:
        stmt = stmt.where(PlexInvite.status == status_filter)
    return [InviteOut(id=i.id, identifier=i.identifier, discord_user_id=i.discord_user_id,
                      discord_username=names.get(i.discord_user_id), invited_by=i.invited_by, verified=i.verified,
                      library_ids=i.library_ids, status=i.status, plex_user_id=i.plex_user_id,
                      created_at=i.created_at, resolved_at=i.resolved_at) for i in db.scalars(stmt)]


@router.delete("/invites/{invite_id}", response_model=InviteOut)
def cancel_invite(invite_id: int, db: DbDep, principal: AdminDep) -> InviteOut:
    """Stop waiting for this invite (WooSarr won't auto-link it). The Plex invite itself stays on plex.tv."""
    inv = db.get(PlexInvite, invite_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    if inv.status == "pending":
        inv.status, inv.resolved_at = "cancelled", utcnow()
        audit.record(db, principal.actor, "invite_cancelled", "discord_user", inv.discord_user_id, invite_id=inv.id)
    db.flush()
    return next(i for i in list_invites(db) if i.id == invite_id)


class MemberOut(BaseModel):
    discord_user_id: str
    username: str
    display_name: str | None
    in_guild: bool
    linked_plex_user_id: str | None


@router.get("/discord/members", response_model=list[MemberOut])
def discord_members(db: DbDep, q: Annotated[str | None, Query(max_length=100)] = None, limit: int = 50) -> list[MemberOut]:
    links = dict(db.execute(select(DiscordLink.discord_user_id, DiscordLink.plex_user_id)).all())
    stmt = select(DiscordMember).order_by(DiscordMember.username).limit(min(max(limit, 1), 200))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            DiscordMember.username.ilike(like) | DiscordMember.display_name.ilike(like)
            | DiscordMember.discord_user_id.like(like)
        )
    return [
        MemberOut(discord_user_id=m.discord_user_id, username=m.username, display_name=m.display_name,
                  in_guild=m.in_guild, linked_plex_user_id=links.get(m.discord_user_id))
        for m in db.scalars(stmt)
    ]
