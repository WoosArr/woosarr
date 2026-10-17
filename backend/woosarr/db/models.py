"""SQLAlchemy models for the plan section 5 data model.

Never store Plex/Discord tokens here except in `secrets` (encrypted, D5).
`discord_links` deliberately has no token column (N9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite drops tzinfo; store UTC and hand back aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use woosarr.db.models.utcnow()")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UTCDateTime, dict[str, Any]: JSON, list[str]: JSON}


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Secret(Base):
    __tablename__ = "secrets"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    # NULL for SAML-only accounts.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    auth_source: Mapped[str] = mapped_column(String(10), default="local")  # local | saml
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None]


class Session(Base):
    __tablename__ = "sessions"
    # sha256 of the cookie value; the raw value only ever lives in the browser.
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class ApiKey(Base):
    """Bearer keys for scripted access (e.g. reading logs remotely, D16)."""

    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)  # shown in UI to tell keys apart
    key_hash: Mapped[str] = mapped_column(String(64))  # sha256 of the full key
    # read: GET requests only (diagnostics); admin: everything an admin can do.
    scope: Mapped[str] = mapped_column(String(10), default="read")
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class PlexUser(Base):
    __tablename__ = "plex_users"
    plex_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    is_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    # The Plex account that owns the server: linkable, never synced.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    thumb: Mapped[str | None] = mapped_column(String(512))
    # No longer a friend/home user on the Plex side.
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class DiscordLink(Base):
    __tablename__ = "discord_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plex_user_id: Mapped[str] = mapped_column(
        ForeignKey("plex_users.plex_user_id", ondelete="CASCADE"), unique=True
    )
    discord_user_id: Mapped[str] = mapped_column(String(32), unique=True)
    method: Mapped[str] = mapped_column(String(10))  # username | oauth | admin
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_at: Mapped[datetime] = mapped_column(default=utcnow)
    linked_by: Mapped[str] = mapped_column(String(100))  # "self" or "admin:<username>"


class DiscordRole(Base):
    __tablename__ = "discord_roles_cache"
    role_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)  # bot/integration role
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class DiscordMember(Base):
    __tablename__ = "discord_members_cache"
    discord_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(100))
    role_ids: Mapped[list[str]] = mapped_column(default=list)
    in_guild: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Library(Base):
    __tablename__ = "libraries"
    # Section key on the configured server (D9; machine id is in settings, D11).
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))
    is_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RoleLibrary(Base):
    __tablename__ = "role_library_map"
    role_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_id: Mapped[str] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), primary_key=True
    )


class ManualGrant(Base):
    __tablename__ = "manual_grants"
    plex_user_id: Mapped[str] = mapped_column(
        ForeignKey("plex_users.plex_user_id", ondelete="CASCADE"), primary_key=True
    )
    library_id: Mapped[str] = mapped_column(
        ForeignKey("libraries.library_id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[str] = mapped_column(String(100))
    granted_at: Mapped[datetime] = mapped_column(default=utcnow)


class LibraryState(Base):
    """Last observed live share state (what Plex said, not what we want)."""

    __tablename__ = "library_state"
    plex_user_id: Mapped[str] = mapped_column(
        ForeignKey("plex_users.plex_user_id", ondelete="CASCADE"), primary_key=True
    )
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))  # admin:<name> | discord:<id> | system
    action: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[str | None] = mapped_column(String(100), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
