"""Pure desired-state calculator for role -> library sync (plan section 5).

No I/O here. Everything is plain data in, plain data out, so it can be unit
tested without Plex or Discord.

Rules implemented (N3-N6):
  * Role grants are additive: a user's role libraries are the union of every
    role they hold. Removing one role never removes a library another role,
    a manual grant, or an exemption still keeps.
  * Exempt libraries are never added or removed by role logic; whatever the
    user currently has stays.
  * Libraries that no role maps to ("unmanaged") are left exactly as found.
  * Manual grants are always part of the target.
  * Only users with a linked Discord account are touched (N5).
  * The result is always the COMPLETE target set (N6); callers write the full
    set, never a partial one. An empty target for a user who currently has
    access is flagged remove_all so the caller uses the dedicated "remove all
    shares" path instead of sending an empty list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

LibraryId = str
RoleId = str


@dataclass(frozen=True)
class UserState:
    """Everything the engine needs to know about one Plex user."""

    plex_user_id: str
    linked: bool
    # Discord role ids the linked member currently holds. Empty if the member
    # left the server and the policy is "drop role-derived access".
    role_ids: frozenset[RoleId] = frozenset()
    manual: frozenset[LibraryId] = frozenset()
    current: frozenset[LibraryId] = frozenset()


@dataclass(frozen=True)
class Plan:
    plex_user_id: str
    target: frozenset[LibraryId]
    add: frozenset[LibraryId]
    remove: frozenset[LibraryId]
    remove_all: bool
    # library id -> short human reason, for the audit log
    reasons: Mapping[LibraryId, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove)


def managed_universe(
    role_map: Mapping[RoleId, Iterable[LibraryId]], exempt: Iterable[LibraryId]
) -> frozenset[LibraryId]:
    """Libraries the role system is allowed to add/remove."""
    all_mapped: set[LibraryId] = set()
    for libs in role_map.values():
        all_mapped.update(libs)
    return frozenset(all_mapped - set(exempt))


def plan_for_user(
    user: UserState,
    role_map: Mapping[RoleId, Iterable[LibraryId]],
    exempt: Iterable[LibraryId],
) -> Plan | None:
    """Return the change plan for one user, or None if they must be skipped."""
    if not user.linked:  # N5
        return None

    exempt_set = frozenset(exempt)
    universe = managed_universe(role_map, exempt_set)

    role_libs: set[LibraryId] = set()
    for role_id in user.role_ids:
        role_libs.update(role_map.get(role_id, ()))  # unknown/deleted roles grant nothing

    desired_managed = (role_libs | set(user.manual)) & universe
    # Anything the user has that is exempt or that no role manages stays put.
    preserved = {lib for lib in user.current if lib in exempt_set or lib not in universe}
    target = frozenset(desired_managed | preserved | set(user.manual))

    add = target - user.current
    remove = user.current - target

    reasons: dict[LibraryId, str] = {}
    for lib in add:
        if lib in user.manual:
            reasons[lib] = "manual grant"
        else:
            reasons[lib] = "granted by role"
    for lib in remove:
        reasons[lib] = "no role or manual grant keeps it"

    return Plan(
        plex_user_id=user.plex_user_id,
        target=target,
        add=frozenset(add),
        remove=frozenset(remove),
        remove_all=(not target and bool(user.current)),
        reasons=reasons,
    )


def plan_all(
    users: Iterable[UserState],
    role_map: Mapping[RoleId, Iterable[LibraryId]],
    exempt: Iterable[LibraryId],
) -> list[Plan]:
    """Plans for every linked user; unlinked users are skipped (N5)."""
    exempt_set = frozenset(exempt)
    plans: list[Plan] = []
    for user in users:
        plan = plan_for_user(user, role_map, exempt_set)
        if plan is not None:
            plans.append(plan)
    return plans
