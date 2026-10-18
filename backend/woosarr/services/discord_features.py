"""Which bot features need which Discord permissions (docs/discord-notes.md).

Pure data + functions so the UI, the invite-URL builder, the docs and the
feature gating all agree. A feature whose permissions the bot lacks is
reported unavailable and its actions are refused with the missing names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Discord permission bits (https://discord.com/developers/docs/topics/permissions)
PERMISSION_BITS: dict[str, int] = {
    "manage_channels": 1 << 4,
    "add_reactions": 1 << 6,
    "view_channel": 1 << 10,
    "embed_links": 1 << 14,
    "send_messages": 1 << 11,
    "read_message_history": 1 << 16,
    "manage_roles": 1 << 28,
    "pin_messages": 1 << 51,
}

PERMISSION_LABELS: dict[str, str] = {
    "manage_channels": "Manage Channels",
    "add_reactions": "Add Reactions",
    "view_channel": "View Channels",
    "embed_links": "Embed Links",
    "send_messages": "Send Messages",
    "read_message_history": "Read Message History",
    "manage_roles": "Manage Roles",
    "pin_messages": "Pin Messages",
}


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    permissions: tuple[str, ...]
    # The bot's highest role must be above the roles it assigns.
    needs_role_hierarchy: bool = False


FEATURES: tuple[Feature, ...] = (
    Feature("commands", "/link, /unlink, /invite, /request",
            "Slash commands with private replies (the request command's name is set in Settings → Requests). "
            "Needs only the applications.commands scope. Request updates are sent as DMs, which need no permission.",
            ()),
    Feature("link_roles", "Link-status roles",
            "Give members the Verified / Unverified / Linked roles that match their Plex link.",
            ("manage_roles",), needs_role_hierarchy=True),
    Feature("create_roles", "Create roles",
            "Create roles from WooSarr (link-status roles, a role per library).", ("manage_roles",)),
    Feature("reaction_posts", "Reaction posts",
            "Post a message with reactions; reacting gives the member Discord roles, removing the reaction "
            "takes them away (libraries follow from the role mappings).",
            ("view_channel", "send_messages", "add_reactions", "read_message_history", "manage_roles"),
            needs_role_hierarchy=True),
    Feature("request_posts", "Public requests",
            "Post requests in the requests channel mentioning the member, reply there and update the post "
            "when the status changes (Settings → Requests → Public).",
            ("view_channel", "send_messages", "embed_links", "read_message_history")),
    Feature("pin_posts", "Pin reaction posts", "Pin the reaction post in its channel.", ("pin_messages",)),
    Feature("create_channel", "Create the reaction channel",
            "Create the channel for a reaction post if it doesn't exist.", ("manage_channels",)),
)
FEATURE_KEYS = frozenset(f.key for f in FEATURES)


def permissions_value(feature_keys: Iterable[str]) -> int:
    """Permission integer for an invite URL that enables exactly these features."""
    value = 0
    wanted = set(feature_keys)
    for f in FEATURES:
        if f.key in wanted:
            for p in f.permissions:
                value |= PERMISSION_BITS[p]
    return value


def feature_report(
    granted: Mapping[str, bool], hierarchy_ok: bool | None | Mapping[str, bool | None] = None
) -> list[dict[str, object]]:
    """Availability of each feature given the bot's guild permissions.

    hierarchy_ok: whether the bot's top role is above the roles a feature assigns,
    as one flag for all features or per feature key (None = nothing to check).
    """
    out = []
    for f in FEATURES:
        missing = [PERMISSION_LABELS[p] for p in f.permissions if not granted.get(p, False)]
        notes = []
        ok = hierarchy_ok.get(f.key) if isinstance(hierarchy_ok, Mapping) else hierarchy_ok
        if f.needs_role_hierarchy and ok is False:
            notes.append("move the bot's role above the roles it should assign (Server Settings → Roles)")
        out.append({
            "key": f.key, "label": f.label, "description": f.description,
            "permissions": [PERMISSION_LABELS[p] for p in f.permissions],
            "available": not missing and not notes, "missing": missing, "notes": notes,
        })
    return out


def available(report: list[dict[str, object]], key: str) -> tuple[bool, str]:
    for f in report:
        if f["key"] == key:
            if f["available"]:
                return True, ""
            parts = []
            if f["missing"]:
                parts.append("missing permission: " + ", ".join(f["missing"]))  # type: ignore[arg-type]
            parts.extend(f["notes"])  # type: ignore[arg-type]
            return False, f"{f['label']} unavailable ({'; '.join(parts)})"
    raise KeyError(key)


def desired_link_roles(
    members: Mapping[str, frozenset[str]],
    links: Mapping[str, bool],
    verified_role: str | None,
    unverified_role: str | None,
    linked_role: str | None,
) -> dict[str, tuple[set[str], set[str]]]:
    """Role changes per member: discord_user_id -> (add, remove).

    members: discord_user_id -> current role ids (members in the guild only)
    links:   discord_user_id -> verified flag (only linked members)
    Only the configured link-status roles are ever touched.
    """
    changes: dict[str, tuple[set[str], set[str]]] = {}
    for uid, roles in members.items():
        want: set[str] = set()
        managed = {r for r in (verified_role, unverified_role, linked_role) if r}
        if uid in links:
            if linked_role:
                want.add(linked_role)
            if links[uid] and verified_role:
                want.add(verified_role)
            if not links[uid] and unverified_role:
                want.add(unverified_role)
        add = want - roles
        remove = (managed - want) & roles
        if add or remove:
            changes[uid] = (add, remove)
    return changes
