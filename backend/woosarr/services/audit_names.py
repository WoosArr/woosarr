"""Names for the ids in audit entries: who did it, what it was done to, and the ids in its details.

Each entry keeps the names as they were when it was written (AuditLog.names, keyed "<kind>:<id>"),
so a renamed role or a deleted member still reads as it did then. Kinds:
- discord: any 17-20 digit snowflake (members, roles, channels, the server, the bot), wherever it is;
- library, plex, admin, post, api_key: short ids, recognised by the field they're in (FIELD_KINDS).

backfill() names the entries written before names were stored (one-time, at startup).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import names as live_names
from woosarr.db.models import (
    AdminUser,
    ApiKey,
    AuditLog,
    DiscordMember,
    DiscordRole,
    Library,
    PlexUser,
    ReactionPost,
)
from woosarr.services.friendly_names import clean

log = logging.getLogger("woosarr.audit")

SNOWFLAKE = re.compile(r"^\d{17,20}$")
_SHORT = re.compile(r"^\d{1,16}$")

# Detail fields holding short ids, by kind (snowflakes are found anywhere).
FIELD_KINDS: dict[str, str] = {
    **dict.fromkeys(("library_ids", "kept_library_ids", "manual_library_ids", "manual_removed", "manual_restored",
                     "removed", "restored", "not_shared", "kept_by_roles", "libraries"), "library"),
    **dict.fromkeys(("plex_user_ids", "plex_user_id"), "plex"),
    "reaction_posts": "post",
}
TARGET_KINDS = {"plex_user": "plex", "plex_users": "plex", "library": "library", "admin": "admin",
                "reaction_post": "post", "api_key": "api_key"}
# Detail fields that already say what the target is called (deleted things, emoji, servers...).
TARGET_NAME_FIELDS = ("role_name", "name", "title", "username", "bot")


def field_kind(action: str, target_type: str | None, key: str) -> str | None:
    if key in FIELD_KINDS:
        return FIELD_KINDS[key]
    if key in ("add", "remove", "reasons") and target_type == "plex_user":  # a sync plan: libraries
        return "library"
    if key in ("before", "after") and action == "role_mapping_changed":
        return "library"
    return None


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").split(",") if v]


def _actor_ref(actor: str) -> tuple[str, str] | None:
    kind, _, ident = actor.partition(":")
    if kind == "discord" and SNOWFLAKE.match(ident):
        return "discord", ident
    if kind == "admin-id" and ident.isdigit():
        return "admin", ident
    return None


def _target_refs(action: str, target_type: str | None, target_id: str | None) -> list[tuple[str, str]]:
    if not target_type or not target_id or (target_type == "admin" and action == "login_failed"):
        return []  # a failed login's target is the username that was typed
    kind = TARGET_KINDS.get(target_type)
    out = []
    for ident in _split(target_id):
        if SNOWFLAKE.match(ident):
            out.append(("discord", ident))
        elif kind and _SHORT.match(ident):
            out.append((kind, ident))
    return out


def _walk(value: Any, kind: str | None, action: str, target_type: str | None, out: set[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            sub = field_kind(action, target_type, str(k)) or kind
            if k == "reasons" and sub:  # {library id: why}
                out.update((sub, str(lib)) for lib in v if _SHORT.match(str(lib)))
                continue
            _walk(v, sub, action, target_type, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk(v, kind, action, target_type, out)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        if SNOWFLAKE.match(text):
            out.add(("discord", text))
        elif kind and _SHORT.match(text):
            out.add((kind, text))


def refs(actor: str, action: str, target_type: str | None, target_id: str | None,
         details: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Every (kind, id) an entry mentions."""
    out: set[tuple[str, str]] = set(_target_refs(action, target_type, target_id))
    if ref := _actor_ref(actor):
        out.add(ref)
    _walk(details or {}, None, action, target_type, out)
    return out


def lookup(session: Session, wanted: set[tuple[str, str]]) -> dict[str, str]:
    """Names for (kind, id) pairs, keyed "kind:id"; ids nobody knows are left out."""
    by_kind: dict[str, set[str]] = {}
    for kind, ident in wanted:
        by_kind.setdefault(kind, set()).add(ident)
    found: dict[str, str] = {}
    with session.no_autoflush:  # called from audit.record() mid-transaction
        if ids := by_kind.get("discord"):
            known = live_names.snapshot()
            found.update({f"discord:{i}": known[i] for i in ids if i in known})
            missing = [i for i in ids if f"discord:{i}" not in found]
            if missing:
                for m in session.scalars(select(DiscordMember).where(DiscordMember.discord_user_id.in_(missing))):
                    found[f"discord:{m.discord_user_id}"] = f"@{clean(m.display_name or m.username)}"
                for r in session.scalars(select(DiscordRole).where(DiscordRole.role_id.in_(missing))):
                    found[f"discord:{r.role_id}"] = f"role {clean(r.name)}"
        if ids := by_kind.get("library"):
            for lib in session.scalars(select(Library).where(Library.library_id.in_(ids))):
                found[f"library:{lib.library_id}"] = clean(lib.title)
        if ids := by_kind.get("plex"):
            for u in session.scalars(select(PlexUser).where(PlexUser.plex_user_id.in_(ids))):
                found[f"plex:{u.plex_user_id}"] = f"Plex {clean(u.username)}"
        for kind, model, attr in (("admin", AdminUser, "username"), ("post", ReactionPost, "title"),
                                  ("api_key", ApiKey, "name")):
            if ids := by_kind.get(kind):
                for row in session.scalars(select(model).where(model.id.in_([int(i) for i in ids]))):
                    found[f"{kind}:{row.id}"] = clean(getattr(row, attr))
    return found


def names_for(session: Session, actor: str, action: str, target_type: str | None, target_id: str | None,
              details: dict[str, Any] | None) -> dict[str, str]:
    return lookup(session, refs(actor, action, target_type, target_id, details))


# --- Presenting an entry -----------------------------------------------------------------------------

def _named(kind: str, ident: str, names: dict[str, str]) -> str:
    name = names.get(f"{kind}:{ident}")
    return f"{name} ({ident})" if name else ident


def actor_label(actor: str, names: dict[str, str]) -> str:
    if actor == "system":
        return "WooSarr"
    if actor.startswith("admin:"):
        return actor[6:]
    if ref := _actor_ref(actor):
        name = names.get(f"{ref[0]}:{ref[1]}")
        return name or ("A member" if ref[0] == "discord" else actor)
    return actor


def target_label(action: str, target_type: str | None, target_id: str | None, details: dict[str, Any] | None,
                 names: dict[str, str]) -> str | None:
    if not target_type:
        return None
    if not target_id:
        return target_type.replace("_", " ")
    named = [names[f"{k}:{i}"] for k, i in _target_refs(action, target_type, target_id) if f"{k}:{i}" in names]
    if named:
        return ", ".join(named)
    for field in TARGET_NAME_FIELDS:
        value = (details or {}).get(field)
        if isinstance(value, str) and value:
            return value
    return f"{target_type.replace('_', ' ')} {target_id}"


def _annotate(value: Any, kind: str | None, action: str, target_type: str | None, names: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            sub = field_kind(action, target_type, str(k)) or kind
            if k == "reasons" and sub and isinstance(v, dict):
                out[k] = {_named(sub, str(lib), names): why for lib, why in v.items()}
            else:
                out[k] = _annotate(v, sub, action, target_type, names)
        return out
    if isinstance(value, (list, tuple)):
        return [_annotate(v, kind, action, target_type, names) for v in value]
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        if SNOWFLAKE.match(text):
            return _named("discord", text, names) if f"discord:{text}" in names else value
        if kind and _SHORT.match(text) and f"{kind}:{text}" in names:
            return _named(kind, text, names)
    return value


def details_named(action: str, target_type: str | None, details: dict[str, Any] | None,
                  names: dict[str, str]) -> dict[str, Any]:
    """The details with "Name (id)" in place of each id that has a name."""
    return _annotate(details or {}, None, action, target_type, names)


# --- One-time update of entries written before names were stored -----------------------------------

def backfill(db: Any, batch: int = 500) -> int:
    """Names every entry that has none yet (written before 1.2), with today's names. Returns how many."""
    done = 0
    while True:
        with db.session() as s:
            rows = list(s.scalars(select(AuditLog).where(AuditLog.names.is_(None)).order_by(AuditLog.id).limit(batch)))
            for row in rows:
                row.names = names_for(s, row.actor, row.action, row.target_type, row.target_id, row.details)
        done += len(rows)
        if len(rows) < batch:
            break
    if done:
        log.info("audit trail: added names to %d older entries (as the names are now)", done)
    return done


async def run_backfill(db: Any) -> None:
    """Startup: name the older entries in a worker thread; the app is usable meanwhile."""
    try:
        await asyncio.to_thread(backfill, db)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("audit trail: naming older entries failed; their ids show without names")
