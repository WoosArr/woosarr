"""SQLAlchemy models for the plan section 5 data model.

Never store Plex/Discord tokens here except in `secrets` (encrypted, D5).
`discord_links` deliberately has no token column (N9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    auth_source: Mapped[str] = mapped_column(String(10), default="local")  # local | oidc
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None]


class OidcProvider(Base):
    """An OpenID Connect identity provider admins can sign in with (D21)."""

    __tablename__ = "oidc_providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)  # used in the login URL
    name: Mapped[str] = mapped_column(String(100))  # button text: "Sign in with <name>"
    kind: Mapped[str] = mapped_column(String(10))  # okta | entra | google | adfs | custom
    issuer: Mapped[str] = mapped_column(String(300))  # as configured / built from the preset
    discovered_issuer: Mapped[str | None] = mapped_column(String(300))  # from .well-known (validated)
    client_id: Mapped[str] = mapped_column(String(300))
    # Client secret lives in `secrets` as oidc.client_secret.<id> (encrypted).
    scopes: Mapped[str] = mapped_column(String(300), default="openid email profile")
    username_claim: Mapped[str] = mapped_column(String(100), default="preferred_username")
    email_claim: Mapped[str] = mapped_column(String(100), default="email")
    groups_claim: Mapped[str] = mapped_column(String(200), default="groups")
    allowed_groups: Mapped[list[str]] = mapped_column(default=list)
    allowed_emails: Mapped[list[str]] = mapped_column(default=list)
    allowed_domains: Mapped[list[str]] = mapped_column(default=list)
    require_email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    extra_params: Mapped[dict[str, Any]] = mapped_column(default=dict)  # e.g. {"prompt": "select_account"}
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)  # preset inputs (tenant, domain...)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None]


class OidcIdentity(Base):
    """Links an admin to a provider account by its stable subject (never by email)."""

    __tablename__ = "oidc_identities"
    __table_args__ = (UniqueConstraint("provider_id", "subject"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("oidc_providers.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
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


class PlexInvite(Base):
    """A Plex friend invite sent via /invite, linked to a Discord member once accepted."""

    __tablename__ = "plex_invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Email or Plex username the invite was sent to.
    identifier: Mapped[str] = mapped_column(String(255), index=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    invited_by: Mapped[str] = mapped_column(String(100))  # discord:<id> (self) or discord-admin:<id>
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # admin invites link as verified
    library_ids: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | accepted | cancelled
    plex_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]


class ReactionPost(Base):
    """A message the bot posts (and pins); reacting with an emoji gives Discord roles
    (which grant libraries through the normal role mappings)."""

    __tablename__ = "reaction_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(String(32))
    message_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    pin: Mapped[bool] = mapped_column(Boolean, default=True)
    # [{"emoji": "🎬", "role_id": "1234…", "label": "<role name>"}, ...]; one role per reaction
    options: Mapped[list[Any]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Legend under the message: one line per reaction (services/reaction_text.py).
    legend: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    legend_template: Mapped[str] = mapped_column(String(300), default="{emoji} {role} — {libraries}",
                                                 server_default="{emoji} {role} — {libraries}")
    legend_header: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # Exactly what was last posted, to edit the message only when the text changes.
    rendered_content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    published_at: Mapped[datetime | None]


class Reaction(Base):
    """A member's reaction on a reaction post, as Discord reports it."""

    __tablename__ = "reactions"
    post_id: Mapped[int] = mapped_column(ForeignKey("reaction_posts.id", ondelete="CASCADE"), primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    emoji: Mapped[str] = mapped_column(String(100), primary_key=True)
    reacted_at: Mapped[datetime] = mapped_column(default=utcnow)


class MediaRequest(Base):
    """A movie/TV request a member made with the request command, mirrored from Seerr
    so WooSarr can tell the member when it is approved, declined or available."""

    __tablename__ = "media_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seerr_request_id: Mapped[int] = mapped_column(Integer, unique=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    plex_user_id: Mapped[str | None] = mapped_column(String(64))
    seerr_user_id: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(8))  # movie | tv
    tmdb_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    year: Mapped[str] = mapped_column(String(4), default="")
    seasons: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [] = whole movie / all seasons
    # pending | approved | declined | failed | partially_available | available | deleted
    status: Mapped[str] = mapped_column(String(20), default="pending")
    notified_status: Mapped[str | None] = mapped_column(String(20))
    # Public requests: the post in the requests channel (status updates reply to it).
    channel_id: Mapped[str | None] = mapped_column(String(32))
    message_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    checked_at: Mapped[datetime | None]


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
