"""Forget what still points at libraries removed from Plex and roles deleted in Discord (P3-08).

A library gone from Plex can't be shared any more, so its role mappings, manual grants, observed
shares and dashboard filters are dropped as soon as the inventory notices (every full sync). The
Library row stays, flagged removed, so the audit trail keeps its name.

A role deleted in Discord is different: while its mappings exist, the sync still manages those
libraries and takes them away from the members who had them only through the role. So its
references (mappings, settings, reaction post options, dashboard filters, the cached role) are
dropped only after a full live sync has run cleanly with them in place; in dry-run they stay, and
the Roles page lists them as deleted.

Everything forgotten is audited (library_forgotten, role_removed) with what it pointed at.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from woosarr.core import app_settings
from woosarr.db.models import (
    DiscordMember,
    DiscordRole,
    Library,
    LibraryState,
    ManualGrant,
    ReactionPost,
    RoleLibrary,
    Setting,
)
from woosarr.services import audit

log = logging.getLogger("woosarr.cleanup")

LINK_ROLE_SETTINGS = ("verified_role_id", "unverified_role_id", "linked_role_id")
LIST_ROLE_SETTINGS = ("invite_role_ids", "request_role_ids", "username_link_role_ids")
LAYOUT_PREFIX = "dashboard.layout."


# ---- dashboard filters -------------------------------------------------------------------------

def _scrub_layouts(session: Session, field: str, ids: set[str]) -> int:
    """Take ids out of every admin's widget filters (field: libraries | roles). A filter left empty
    is dropped, so the copy shows everything again. Returns how many widgets changed."""
    changed = 0
    for row in session.scalars(select(Setting).where(Setting.key.startswith(LAYOUT_PREFIX))):
        widgets = row.value if isinstance(row.value, list) else []
        out, touched = [], False
        for w in widgets:
            f = w.get("filter") if isinstance(w, dict) else None
            if isinstance(f, dict) and set(f.get(field) or []) & ids:
                f = dict(f, **{field: [i for i in f[field] if i not in ids]})
                w = dict(w, filter=f if any(f.values()) else None)
                touched = True
                changed += 1
            out.append(w)
        if touched:
            row.value = out
    return changed


# ---- libraries ---------------------------------------------------------------------------------

def forget_removed_libraries(session: Session) -> list[dict[str, Any]]:
    """Drop the references to libraries flagged removed. Returns what was dropped, per library."""
    gone = {lib.library_id: lib for lib in session.scalars(select(Library).where(Library.removed.is_(True)))}
    if not gone:
        return []
    ids = set(gone)
    roles: dict[str, list[str]] = {}
    for row in session.scalars(select(RoleLibrary).where(RoleLibrary.library_id.in_(ids))):
        roles.setdefault(row.library_id, []).append(row.role_id)
    grants: dict[str, list[str]] = {}
    for row in session.scalars(select(ManualGrant).where(ManualGrant.library_id.in_(ids))):
        grants.setdefault(row.library_id, []).append(row.plex_user_id)
    shares = set(session.scalars(select(LibraryState.library_id).where(LibraryState.library_id.in_(ids))))
    widgets = _scrub_layouts(session, "libraries", ids)
    touched = set(roles) | set(grants) | shares
    if not touched and not widgets:
        return []
    session.execute(delete(RoleLibrary).where(RoleLibrary.library_id.in_(ids)))
    session.execute(delete(ManualGrant).where(ManualGrant.library_id.in_(ids)))
    session.execute(delete(LibraryState).where(LibraryState.library_id.in_(ids)))
    out = []
    for lib_id in sorted(touched):
        lib = gone[lib_id]
        entry = {"library_id": lib_id, "title": lib.title, "role_ids": sorted(roles.get(lib_id, [])),
                 "manual_grants": sorted(grants.get(lib_id, []))}
        audit.record(session, "system", "library_forgotten", "library", lib_id, title=lib.title,
                     role_ids=entry["role_ids"], plex_user_ids=entry["manual_grants"])
        log.info("library %s (%s) is gone from Plex: removed its %d role mapping(s) and %d manual grant(s)",
                 lib_id, lib.title, len(entry["role_ids"]), len(entry["manual_grants"]))
        out.append(entry)
    if widgets:
        log.info("removed libraries %s from %d dashboard widget filter(s)", sorted(ids), widgets)
    return out


# ---- roles -------------------------------------------------------------------------------------

def _option_roles(option: dict) -> set[str]:
    return {str(option["role_id"])} if option.get("role_id") else {str(r) for r in option.get("role_ids") or []}


def role_references(session: Session, role_id: str) -> dict[str, Any] | None:
    """What still refers to a role (None: WooSarr knows nothing about it)."""
    role = session.get(DiscordRole, role_id)
    libs = sorted(session.scalars(select(RoleLibrary.library_id).where(RoleLibrary.role_id == role_id)))
    if role is None and not libs:
        return None
    st = app_settings.load(session)
    settings = [k for k in LINK_ROLE_SETTINGS if getattr(st, k) == role_id]
    settings += [k for k in LIST_ROLE_SETTINGS if role_id in getattr(st, k)]
    posts = [{"id": p.id, "title": p.title, "emoji": o["emoji"]} for p in session.scalars(select(ReactionPost))
             for o in p.options or [] if role_id in _option_roles(o)]
    holding = sum(1 for m in session.scalars(select(DiscordMember)) if role_id in (m.role_ids or []))
    return {"role_id": role_id, "name": role.name if role else f"(unknown role {role_id})",
            "deleted_in_discord": role is None or role.deleted, "library_ids": libs, "settings": settings,
            "reaction_posts": posts, "members_holding": holding}


def deleted_role_ids(session: Session) -> list[str]:
    """Roles flagged deleted, and mapped role ids WooSarr has never seen in Discord."""
    known = {r.role_id: r for r in session.scalars(select(DiscordRole))}
    ids = {rid for rid, r in known.items() if r.deleted}
    ids |= {rid for rid in session.scalars(select(RoleLibrary.role_id)) if rid not in known}
    return sorted(ids)


def purge_role(session: Session, role_id: str, actor: str) -> dict[str, Any]:
    """Forget a role that no longer exists in Discord: mappings, settings, reaction options, filters."""
    refs = role_references(session, role_id)
    if refs is None or not refs["deleted_in_discord"]:
        raise ValueError(f"role {role_id} still exists in Discord")
    session.execute(delete(RoleLibrary).where(RoleLibrary.role_id == role_id))
    st = app_settings.load(session)
    changes: dict = {k: None for k in refs["settings"] if k in LINK_ROLE_SETTINGS}
    changes |= {k: [r for r in getattr(st, k) if r != role_id] for k in refs["settings"] if k in LIST_ROLE_SETTINGS}
    if changes:
        app_settings.update(session, changes)
    for post in session.scalars(select(ReactionPost)):
        kept = [o for o in post.options or [] if role_id not in _option_roles(o)]
        if len(kept) != len(post.options or []):
            post.options = kept
    for m in session.scalars(select(DiscordMember)):
        if role_id in (m.role_ids or []):
            m.role_ids = [r for r in m.role_ids if r != role_id]
    widgets = _scrub_layouts(session, "roles", {role_id})
    role = session.get(DiscordRole, role_id)
    if role is not None:
        session.delete(role)
    audit.record(session, actor, "role_removed", "role", role_id, role_name=refs["name"],
                 library_ids=refs["library_ids"], settings=refs["settings"],
                 reaction_posts=[p["id"] for p in refs["reaction_posts"]], dashboard_filters=widgets)
    log.info("role %s (%s) deleted in Discord: forgot %d library mapping(s), settings %s, %d reaction option(s), "
             "%d dashboard filter(s) (by %s)", role_id, refs["name"], len(refs["library_ids"]), refs["settings"] or "-",
             len(refs["reaction_posts"]), widgets, actor)
    return refs


def forget_deleted_roles(session: Session) -> list[dict[str, Any]]:
    """After a clean full live sync: purge the roles Discord reported deleted. Mapped ids WooSarr has
    never seen in Discord (e.g. mapped before the bot first connected) are left for the Roles page."""
    ids = session.scalars(select(DiscordRole.role_id).where(DiscordRole.deleted.is_(True)).order_by(DiscordRole.role_id))
    return [purge_role(session, rid, "system") for rid in list(ids)]
