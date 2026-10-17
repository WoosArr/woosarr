"""Admin-editable settings stored in the database (plan P7-05, D12, Q1).

Unlike core/config.py (process env), these change at runtime from the UI. They
are one validated JSON document under settings.key = "app".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from woosarr.db.models import Setting

KEY = "app"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A fresh install only logs intended changes until an admin turns this off.
    dry_run: bool = True
    reconcile_interval_minutes: int = Field(15, ge=5, le=1440)
    # Members who leave the Discord server: drop role-derived access (keep manual
    # + exempt), or leave their access untouched.
    leave_policy: Literal["drop_role_access", "keep_all"] = "drop_role_access"
    # Members who run /unlink: same choices as leave_policy.
    unlink_policy: Literal["drop_role_access", "keep_all"] = "drop_role_access"
    # Discord roles whose members may /invite themselves (admins can always invite).
    invite_role_ids: list[str] = Field(default_factory=list, max_length=50)
    # Every member may /invite themselves (overrides invite_role_ids).
    invite_all_members: bool = False
    # Roles the bot keeps in step with each member's Plex link (None = off).
    verified_role_id: str | None = Field(None, pattern=r"^\d{5,25}$")
    unverified_role_id: str | None = Field(None, pattern=r"^\d{5,25}$")
    linked_role_id: str | None = Field(None, pattern=r"^\d{5,25}$")
    # R4: optionally treat username-only (unverified) links as not linked.
    ignore_unverified_links: bool = False
    link_cooldown_seconds: int = Field(300, ge=0, le=86400)
    # None = use WOOSARR_LOG_LEVEL.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    audit_retention_days: int = Field(365, ge=7, le=3650)
    # Local password login; can only be turned off once an SSO admin has signed in.
    local_login_enabled: bool = True
    # Public address of WooSarr (e.g. https://woosarr.example.com) for SSO redirect
    # URIs; empty = the address the browser used.
    public_url: str = Field("", max_length=300, pattern=r"^(|https?://[^\s/]+(/[^\s]*)?)$")


def load(session: Session) -> AppSettings:
    row = session.get(Setting, KEY)
    return AppSettings.model_validate(row.value) if row else AppSettings()


def update(session: Session, changes: dict[str, Any]) -> AppSettings:
    """Validate the merged document; nothing is written if validation fails."""
    merged = load(session).model_dump() | changes
    new = AppSettings.model_validate(merged)
    row = session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=new.model_dump()))
    else:
        row.value = new.model_dump()
    return new
