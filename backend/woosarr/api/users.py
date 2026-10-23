"""Users page API: Plex users, links, roles, access sources, manual grants (P7-01, P4-06)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

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
from woosarr.services import audit, impact, link_service
from woosarr.services.access import library_sources, load_snapshot, user_state
from woosarr.services.discord_bot import DiscordConfigError

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
    sources: list[Literal["role", "manual", "exempt", "external"]]
    shared: bool  # currently shared on Plex (last observed)


class UserOut(BaseModel):
    plex_user_id: str
    username: str
    email: str | None
    is_managed: bool
    is_owner: bool
    removed: bool
    link: LinkOut | None  # the main member: the first verified one, else the first
    links: list[LinkOut]  # every linked member (several when a family shares the Plex account)
    roles: list[RoleRef]  # union of the roles of members in the Discord server
    libraries: list[LibraryAccess]
    manual_library_ids: list[str]


def _link_out(link: DiscordLink, member: DiscordMember | None) -> LinkOut:
    return LinkOut(
        discord_user_id=link.discord_user_id,
        discord_username=member.username if member else None,
        discord_display_name=member.display_name if member else None,
        in_guild=bool(member and member.in_guild),
        method=link.method,
        verified=link.verified,
        linked_at=link.linked_at,
        linked_by=link.linked_by,
    )


def _users(db, only: str | None = None) -> list[UserOut]:
    settings = app_settings.load(db)
    snap = load_snapshot(db)
    roles = {r.role_id: r for r in db.scalars(select(DiscordRole))}
    titles = dict(db.execute(select(Library.library_id, Library.title)).all())
    stmt = select(PlexUser).order_by(func.lower(PlexUser.username), PlexUser.username)
    if only:
        stmt = stmt.where(PlexUser.plex_user_id == only)
    out = []
    for user in db.scalars(stmt):
        current = snap.cached_shares.get(user.plex_user_id, frozenset())
        links = snap.links.get(user.plex_user_id, [])
        link_outs = [_link_out(x, snap.members.get(x.discord_user_id)) for x in links]
        main = next((x for x in link_outs if x.verified), link_outs[0] if link_outs else None)
        member_roles: list[str] = []
        for x in links:
            member = snap.members.get(x.discord_user_id)
            if member and member.in_guild:
                member_roles += [r for r in member.role_ids if r not in member_roles]
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
                link=main,
                links=link_outs,
                roles=[
                    RoleRef(role_id=r, name=roles[r].name, color=roles[r].color)
                    for r in member_roles
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
    # linked = verified + unverified
    link: Annotated[Literal["verified", "unverified", "linked", "unlinked"] | None, Query()] = None,
    include_removed: bool = False,
) -> list[UserOut]:
    users = _users(db)
    if not include_removed:
        users = [u for u in users if not u.removed]
    if link == "verified":
        users = [u for u in users if u.link and u.link.verified]
    elif link == "unverified":
        users = [u for u in users if u.link and not u.link.verified]
    elif link == "linked":
        users = [u for u in users if u.link is not None]
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


class RemoveAccessIn(BaseModel):
    plex_user_ids: list[str] = Field(min_length=1, max_length=500)
    library_ids: list[str] = Field(min_length=1, max_length=200)


class RemoveAccessResult(BaseModel):
    plex_user_id: str
    username: str
    removed: list[str]
    kept_by_roles: list[str]
    manual_removed: list[str]
    not_shared: list[str]
    error: str | None
    dry_run: bool


@router.post("/users/remove-access", response_model=list[RemoveAccessResult])
def remove_access(body: RemoveAccessIn, request: Request, db: DbDep, principal: AdminDep) -> list[RemoveAccessResult]:
    """Remove libraries from users in Plex now (and their manual grants for them).

    Libraries a linked user's Discord roles grant are kept and reported, since the
    next sync would add them back; change their roles (or the role mapping) instead.
    """
    names = dict(db.execute(select(PlexUser.plex_user_id, PlexUser.username)
                            .where(PlexUser.plex_user_id.in_(body.plex_user_ids))).all())
    missing = sorted(set(body.plex_user_ids) - set(names))
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown Plex users: {missing[:10]}")
    dry_run = app_settings.load(db).dry_run
    db.commit()  # release the session before the sync worker takes its lock
    results = request.app.state.sync.remove_access(body.plex_user_ids, body.library_ids, principal.actor)
    return [RemoveAccessResult(**r, username=names.get(str(r["plex_user_id"]), ""), dry_run=dry_run) for r in results]


class RemovePreview(BaseModel):
    plex_user_id: str
    username: str
    removed: list[str]
    kept_by_roles: list[str]
    not_shared: list[str]
    owner: bool


@router.post("/users/remove-access/preview", response_model=list[RemovePreview])
def remove_access_preview(body: RemoveAccessIn, db: DbDep) -> list[RemovePreview]:
    """What 'Remove libraries' would do, from the last observed shares (no Plex calls)."""
    return [RemovePreview(**r) for r in impact.removal_preview(db, body.plex_user_ids, body.library_ids)]


class RestoreItem(BaseModel):
    plex_user_id: str
    library_ids: list[str] = Field(default_factory=list, max_length=200)
    manual_library_ids: list[str] = Field(default_factory=list, max_length=200)


class RestoreIn(BaseModel):
    items: list[RestoreItem] = Field(min_length=1, max_length=500)


class RestoreResult(BaseModel):
    plex_user_id: str
    restored: list[str]
    error: str | None


@router.post("/users/restore-access", response_model=list[RestoreResult])
def restore_access(body: RestoreIn, request: Request, db: DbDep, principal: AdminDep) -> list[RestoreResult]:
    """Undo a 'Remove libraries' (gives back what was removed, recreates manual grants)."""
    known = set(db.scalars(select(PlexUser.plex_user_id).where(
        PlexUser.plex_user_id.in_([i.plex_user_id for i in body.items]))))
    libs = {lib for i in body.items for lib in (*i.library_ids, *i.manual_library_ids)}
    known_libs = set(db.scalars(select(Library.library_id).where(Library.library_id.in_(libs))))
    if {i.plex_user_id for i in body.items} - known or libs - known_libs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown users or libraries")
    db.commit()
    results = request.app.state.sync.restore_access([i.model_dump() for i in body.items], principal.actor)
    return [RestoreResult(**r) for r in results]


class AdminLinkIn(BaseModel):
    discord_user_id: Annotated[str, Field(pattern=r"^\d{5,25}$")]


@router.put("/users/{plex_user_id}/link", response_model=UserOut)
def admin_link(plex_user_id: str, body: AdminLinkIn, request: Request, db: DbDep, principal: AdminDep) -> UserOut:
    """Link a Discord member to this Plex account (verified). If other members are already linked,
    this adds one more: a family sharing the account gets the union of their roles."""
    if db.get(PlexUser, plex_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plex user not found")
    by_discord = db.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == body.discord_user_id))
    if by_discord and by_discord.plex_user_id != plex_user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "that Discord account is linked to another Plex user; unlink it first")
    others = [x.discord_user_id for x in db.scalars(select(DiscordLink).where(
        DiscordLink.plex_user_id == plex_user_id, DiscordLink.discord_user_id != body.discord_user_id))]
    if by_discord is None:
        db.add(DiscordLink(plex_user_id=plex_user_id, discord_user_id=body.discord_user_id,
                           method="admin", verified=True, linked_by=principal.actor))
    else:
        by_discord.method, by_discord.verified, by_discord.linked_by = "admin", True, principal.actor
    db.flush()
    audit.record(db, principal.actor, "link_admin", "plex_user", plex_user_id,
                 discord_user_id=body.discord_user_id, other_discord_user_ids=others or None)
    request.app.state.sync.trigger([plex_user_id], f"linked by {principal.actor}")
    return _users(db, plex_user_id)[0]


@router.delete("/users/{plex_user_id}/link", response_model=UserOut)
def admin_unlink(plex_user_id: str, request: Request, db: DbDep, principal: AdminDep,
                 discord_user_id: Annotated[str | None, Query(pattern=r"^\d{5,25}$")] = None) -> UserOut:
    """Unlink a member. With several members (a shared account), name the one to remove; the
    account keeps what the others' roles give, and under unlink_policy keep_all the libraries
    only this member's roles gave stay as manual grants."""
    links = list(db.scalars(select(DiscordLink).where(DiscordLink.plex_user_id == plex_user_id)))
    if not links:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user is not linked")
    if discord_user_id is None:
        if len(links) > 1:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                "several members share this account; say which one (discord_user_id)")
        link = links[0]
    else:
        link = next((x for x in links if x.discord_user_id == discord_user_id), None)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "that member isn't linked to this Plex user")
    discord_id = link.discord_user_id
    kept: list[str] = []
    if len(links) > 1 and app_settings.load(db).unlink_policy == "keep_all":
        kept = link_service.keep_member_libraries(db, plex_user_id, discord_id, principal.actor)
    db.delete(link)
    db.flush()
    remaining = [x.discord_user_id for x in links if x.discord_user_id != discord_id]
    audit.record(db, principal.actor, "unlink_admin", "plex_user", plex_user_id, discord_user_id=discord_id,
                 remaining_discord_user_ids=remaining or None, kept_library_ids=kept or None)
    # The last member gone: no longer managed (N5); otherwise the others' roles decide.
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
    stmt = (select(DiscordMember).where(DiscordMember.is_bot.is_(False))  # bots can't link a Plex account
            .order_by(func.lower(DiscordMember.username), DiscordMember.username).limit(min(max(limit, 1), 200)))
    if q:
        # Literal match: % and _ typed in the search are characters, not wildcards.
        needle = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(
            DiscordMember.username.ilike(like, escape="\\") | DiscordMember.display_name.ilike(like, escape="\\")
            | DiscordMember.discord_user_id.like(like, escape="\\")
        )
    return [
        MemberOut(discord_user_id=m.discord_user_id, username=m.username, display_name=m.display_name,
                  in_guild=m.in_guild, linked_plex_user_id=links.get(m.discord_user_id))
        for m in db.scalars(stmt)
    ]


# ---- Users → Discord: server members and bulk role changes -------------------------------

class MemberLinkOut(BaseModel):
    plex_user_id: str
    plex_username: str | None
    verified: bool
    method: str


class DiscordMemberRow(BaseModel):
    discord_user_id: str
    username: str
    display_name: str | None
    roles: list[RoleRef]
    link: MemberLinkOut | None


@router.get("/discord/member-list", response_model=list[DiscordMemberRow])
def discord_member_list(
    db: DbDep, link: Annotated[Literal["verified", "unverified", "linked", "unlinked"] | None, Query()] = None,
) -> list[DiscordMemberRow]:
    """Members on the Discord server (from the bot's cache), with their roles and Plex link."""
    roles = {r.role_id: r for r in db.scalars(select(DiscordRole).where(DiscordRole.deleted.is_(False)))}
    links = {x.discord_user_id: x for x in db.scalars(select(DiscordLink))}
    plex_names = dict(db.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    out = []
    for m in db.scalars(select(DiscordMember).where(DiscordMember.in_guild.is_(True), DiscordMember.is_bot.is_(False))
                        .order_by(func.lower(func.coalesce(DiscordMember.display_name, DiscordMember.username)),
                                  DiscordMember.username)):
        x = links.get(m.discord_user_id)
        if (link == "verified" and not (x and x.verified)) or (link == "unverified" and not (x and not x.verified)) \
                or (link == "linked" and not x) or (link == "unlinked" and x):
            continue
        member_roles = sorted((roles[r] for r in m.role_ids or [] if r in roles), key=lambda r: -r.position)
        out.append(DiscordMemberRow(
            discord_user_id=m.discord_user_id, username=m.username, display_name=m.display_name,
            roles=[RoleRef(role_id=r.role_id, name=r.name, color=r.color) for r in member_roles],
            link=MemberLinkOut(plex_user_id=x.plex_user_id, plex_username=plex_names.get(x.plex_user_id),
                               verified=x.verified, method=x.method) if x else None))
    return out


# Discord snowflakes (real ones have 17-20 digits; the demo uses short ids), as for reaction-post roles.
RoleId = Annotated[str, Field(pattern=r"^\d{1,25}$")]


class MemberRolesIn(BaseModel):
    # Discord limits role edits per member; 200 at a time keeps a request to a sensible length.
    discord_user_ids: Annotated[list[RoleId], Field(min_length=1, max_length=200)]
    add_role_ids: Annotated[list[RoleId], Field(max_length=50)] = []
    remove_role_ids: Annotated[list[RoleId], Field(max_length=50)] = []


class RoleImpactOut(BaseModel):
    plex_user_id: str
    username: str
    gain: list[str]
    lose: list[str]


@router.post("/discord/members/roles/preview", response_model=list[RoleImpactOut])
def member_roles_preview(body: MemberRolesIn, db: DbDep) -> list[RoleImpactOut]:
    """Which linked Plex accounts would gain or lose libraries (no Discord or Plex calls)."""
    return [RoleImpactOut(**r) for r in impact.role_change_impact(db, body.discord_user_ids, body.add_role_ids,
                                                                  body.remove_role_ids)]


class MemberRolesResult(BaseModel):
    discord_user_id: str
    added: list[str]
    removed: list[str]
    error: str | None


@router.post("/discord/members/roles", response_model=list[MemberRolesResult])
async def member_roles_change(body: MemberRolesIn, request: Request, principal: AdminDep) -> list[MemberRolesResult]:
    """Give and/or take Discord roles for the selected members (the bot does it; audited)."""
    if not body.add_role_ids and not body.remove_role_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "choose roles to add or remove")
    svc = request.app.state.discord
    if svc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Discord isn't available in demo mode")
    try:
        results = await svc.change_member_roles(body.discord_user_ids, body.add_role_ids, body.remove_role_ids,
                                                principal.actor)
    except DiscordConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return [MemberRolesResult(**r) for r in results]
