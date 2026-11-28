"""Config import (D23, P15-02): preview what an export file would change here, then apply the chosen
parts in one transaction.

Preview and apply run the same code (`run(..., apply=False|True)`), so the preview is exactly what an
import does. Rules:

- Libraries: same Plex server (or Plex not connected yet): by id; another Plex server: by title + type.
  Libraries that don't match are left out (warned), and so are their mappings and grants.
- Roles: same Discord server (or not connected yet): by id; another server: by name. Same for the
  roles in reaction-post options.
- Reaction posts keep their Discord message only on the same server with the same bot; otherwise
  they're imported unpublished.
- Admins are added or updated by username, never removed; nobody is locked out by an import.
- Audit entries are appended; ones already here are skipped.
- Tokens and secrets only with the passphrase. After any import dry-run is on, so nothing changes on
  Plex until the owner has looked at the planned changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.core.secret_store import SecretStore
from woosarr.db.models import (
    AdminUser,
    AuditLog,
    DiscordLink,
    DiscordRole,
    Library,
    ManualGrant,
    MediaRequest,
    OidcIdentity,
    OidcProvider,
    PlexUser,
    ReactionPost,
    RoleLibrary,
    utcnow,
)
from woosarr.services.config_transfer import (
    FORMAT,
    K_DISCORD_BOT,
    K_DISCORD_GUILD,
    K_PLEX_ACCOUNT,
    K_PLEX_SERVER,
    K_SEERR,
    KIND,
    PARTS,
    TransferError,
    decrypt_secrets,
)

log = logging.getLogger("woosarr.config_transfer")

MAX_BYTES = 25 * 1024 * 1024  # audit history makes files big; anything larger isn't ours
SECRETS = "secrets"  # chosen on import like a part
IMPORT_PARTS = (*PARTS, SECRETS)
MAX_ROWS = 200_000

# ---- File model (strict: types, lengths, id shapes; unknown fields ignored for later versions) -----


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


def S(n: int) -> Any:
    return Annotated[str, StringConstraints(max_length=n)]


Snowflake = Annotated[str, StringConstraints(pattern=r"^\d{1,25}$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z_.:-]{1,64}$")]
Rows = lambda t: Annotated[list[t], Field(max_length=MAX_ROWS)]


class Source(_M):
    plex_machine_id: S(100) | None = None
    plex_server: S(255) | None = None
    discord_guild_id: Snowflake | None = None
    discord_guild: S(100) | None = None
    plex_account: dict[str, S(255) | None] | None = None
    discord_bot: dict[str, S(255) | None] | None = None


class SettingsPart(_M):
    app: dict[str, Any] = Field(default_factory=dict, max_length=500)
    seerr: dict[str, S(500) | bool | int | None] | None = None


class LibraryIn(_M):
    id: Ident
    title: S(255)
    type: S(32) = ""
    exempt: bool = False
    removed: bool = False


class RoleMapIn(_M):
    role_id: Snowflake
    role_name: S(100) | None = None
    library_ids: Annotated[list[Ident], Field(max_length=10_000)] = Field(default_factory=list)


class LibrariesPart(_M):
    libraries: Rows(LibraryIn) = Field(default_factory=list)
    role_mappings: Rows(RoleMapIn) = Field(default_factory=list)


class GrantIn(_M):
    plex_user_id: Ident
    plex_username: S(255) | None = None
    library_id: Ident
    granted_by: S(100) = "import"
    granted_at: datetime | None = None


class OptionIn(_M):
    emoji: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    role_id: Snowflake
    label: S(100) = ""
    former: Annotated[list[S(100)], Field(max_length=50)] = Field(default_factory=list)


class PostIn(_M):
    title: S(100)
    content: S(4000) = ""
    channel_id: Snowflake | None = None
    message_id: Snowflake | None = None
    pin: bool = True
    enabled: bool = True
    options: Annotated[list[OptionIn], Field(max_length=50)] = Field(default_factory=list)
    legend: bool = True
    legend_template: S(300) = "{emoji} {role} — {libraries}"
    legend_header: S(200) = ""
    link_button: bool = False
    invite_button: bool = False
    published_at: datetime | None = None


class ProviderIn(_M):
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]
    name: S(100)
    kind: Literal["okta", "entra", "google", "adfs", "custom"]
    issuer: Annotated[str, StringConstraints(pattern=r"^https?://", max_length=300)]
    client_id: S(300)
    scopes: S(300) = "openid email profile"
    username_claim: S(100) = "preferred_username"
    email_claim: S(100) = "email"
    groups_claim: S(200) = "groups"
    allowed_groups: Annotated[list[S(300)], Field(max_length=1000)] = Field(default_factory=list)
    allowed_emails: Annotated[list[S(300)], Field(max_length=1000)] = Field(default_factory=list)
    allowed_domains: Annotated[list[S(300)], Field(max_length=1000)] = Field(default_factory=list)
    require_email_verified: bool = True
    extra_params: Annotated[dict[S(100), S(300)], Field(max_length=20)] = Field(default_factory=dict)
    settings: Annotated[dict[S(100), S(300) | bool | int | None], Field(max_length=50)] = Field(default_factory=dict)
    enabled: bool = False


class IdentityIn(_M):
    provider: S(40)
    subject: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    admin: S(100) | None = None
    email: S(255) | None = None


class SsoPart(_M):
    providers: Annotated[list[ProviderIn], Field(max_length=100)] = Field(default_factory=list)
    identities: Rows(IdentityIn) = Field(default_factory=list)


class LinkIn(_M):
    discord_user_id: Snowflake
    plex_user_id: Ident
    plex_username: S(255) | None = None
    method: Literal["username", "oauth", "admin"]
    verified: bool = False
    linked_at: datetime | None = None
    linked_by: S(100) = "import"


class AdminIn(_M):
    username: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    auth_source: Literal["local", "oidc"] = "local"
    active: bool = True
    created_at: datetime | None = None


class RequestIn(_M):
    seerr_request_id: int
    discord_user_id: Snowflake
    plex_user_id: Ident | None = None
    seerr_user_id: int
    media_type: Literal["movie", "tv"]
    tmdb_id: int
    title: S(300)
    year: S(4) = ""
    seasons: Annotated[list[int], Field(max_length=200)] = Field(default_factory=list)
    status: Literal["pending", "approved", "declined", "failed", "partially_available", "available", "deleted"]
    notified_status: S(20) | None = None
    channel_id: Snowflake | None = None
    message_id: Snowflake | None = None
    retries: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    checked_at: datetime | None = None
    status_at: datetime | None = None
    retried_at: datetime | None = None


class AuditIn(_M):
    ts: datetime
    actor: S(100)
    action: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    target_type: S(30) | None = None
    target_id: S(100) | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    names: dict[S(200), S(300)] | None = None


class Parts(_M):
    settings: SettingsPart | None = None
    libraries: LibrariesPart | None = None
    manual_grants: Rows(GrantIn) | None = None
    reaction_posts: Annotated[list[PostIn], Field(max_length=500)] | None = None
    sso: SsoPart | None = None
    links: Rows(LinkIn) | None = None
    admins: Annotated[list[AdminIn], Field(max_length=500)] | None = None
    requests: Rows(RequestIn) | None = None
    audit: Rows(AuditIn) | None = None


class ExportFile(_M):
    kind: Literal["woosarr-config"]
    format: int
    woosarr: S(100) | None = None
    exported_at: S(64) | None = None
    source: Source = Field(default_factory=Source)
    parts: Parts
    secrets: dict[str, Any] | None = None


def parse(raw: Any) -> ExportFile:
    """The export file, validated. TransferError with a short reason otherwise."""
    if not isinstance(raw, dict):
        raise TransferError("this isn't a WooSarr export file (expected a JSON object)")
    if raw.get("kind") != KIND:
        raise TransferError("this isn't a WooSarr export file")
    fmt = raw.get("format")
    if not isinstance(fmt, int) or fmt < 1:
        raise TransferError("the export file has no valid format number")
    if fmt > FORMAT:
        raise TransferError(f"the export file comes from a newer WooSarr ({raw.get('woosarr') or 'unknown version'}); "
                            "update this server first")
    unknown = sorted(set(raw.get("parts") or {}) - set(PARTS)) if isinstance(raw.get("parts"), dict) else []
    if unknown:
        raise TransferError(f"the export file has unknown part(s): {', '.join(unknown[:5])}")
    try:
        return ExportFile.model_validate(raw)
    except ValidationError as exc:
        where = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
        more = f" (and {exc.error_count() - 3} more)" if exc.error_count() > 3 else ""
        raise TransferError(f"the export file is damaged or was edited: {where}{more}") from None


# ---- Run (preview or apply) --------------------------------------------------------------------


@dataclass
class PartResult:
    name: str
    summary: str
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Result:
    parts: list[PartResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    secrets_found: list[str] = field(default_factory=list)  # names (what), never values
    secrets_readable: bool | None = None  # None: no passphrase given
    encrypted_count: int = 0
    plex_token: bool = False  # a Plex token was imported: rebuild the Plex connection
    discord_token: bool = False  # a bot token was imported: restart the bot

    def add(self, name: str, summary: str, **counts: int) -> None:
        self.parts.append(PartResult(name, summary, {k: v for k, v in counts.items() if v}))


SECRET_LABELS = {"plex_token": "Plex token", "discord_bot_token": "Discord bot token", "seerr_api_key": "Seerr API key"}
TOKEN_NAMES = {"plex_token": names.PLEX_TOKEN, "discord_bot_token": names.DISCORD_BOT_TOKEN,
               "seerr_api_key": names.SEERR_API_KEY}


def _secret_label(key: str) -> str:
    if key in SECRET_LABELS:
        return SECRET_LABELS[key]
    kind, _, name = key.partition(":")
    return {"oidc_client_secret": f"Single sign-on client secret ({name})",
            "admin_password_hash": f"Admin password ({name})"}.get(kind, kind)


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    many = word[:-1] + "ies" if word.endswith("y") and word[-2:-1] not in "aeiou" else word + "s"
    return f"{n} {many}"


class _Run:
    def __init__(self, s: Session, store: SecretStore, doc: ExportFile, parts: list[str], passphrase: str | None,
                 apply: bool, actor: str) -> None:
        self.s, self.store, self.doc, self.apply, self.actor = s, store, doc, apply, actor
        self.chosen = [p for p in IMPORT_PARTS if p in parts and (p == SECRETS or getattr(doc.parts, p) is not None)]
        self.r = Result()
        src = doc.source
        here_server = kv.get(s, K_PLEX_SERVER) or {}
        here_guild = kv.get(s, K_DISCORD_GUILD) or {}
        here_bot = kv.get(s, K_DISCORD_BOT) or {}
        self.here_machine = here_server.get("machine_id")
        self.here_guild = here_guild.get("id")
        self.same_plex = not self.here_machine or not src.plex_machine_id or src.plex_machine_id == self.here_machine
        self.same_guild = not self.here_guild or not src.discord_guild_id or src.discord_guild_id == self.here_guild
        src_bot = (src.discord_bot or {}).get("id")
        self.same_bot = self.same_guild and (not here_bot.get("id") or not src_bot or here_bot.get("id") == src_bot)
        self.values: dict[str, str] = {}
        self._secrets(passphrase)
        self.lib_map: dict[str, str] = {}
        self.role_map: dict[str, str] = {}
        self._maps()

    def warn(self, text: str) -> None:
        if text not in self.r.warnings:
            self.r.warnings.append(text)

    # -- secrets and maps ---------------------------------------------------------------------

    def _secrets(self, passphrase: str | None) -> None:
        block = self.doc.secrets
        if not block:
            if SECRETS in self.chosen:
                self.warn("The file has no tokens or secrets; enter them again on the Setup page.")
            return
        if not passphrase:
            if SECRETS in self.chosen:
                self.warn("The file has encrypted tokens and secrets; enter the passphrase to import them.")
            return
        try:
            self.values = decrypt_secrets(block, passphrase)
            self.r.secrets_readable = True
        except TransferError:
            self.r.secrets_readable = False
            if self.apply and SECRETS in self.chosen:
                raise
            self.warn("The tokens and secrets couldn't be opened: wrong passphrase, or the file was damaged.")
            return
        self.r.secrets_found = [_secret_label(k) for k in sorted(self.values)]

    def _maps(self) -> None:
        parts = self.doc.parts
        here_libs = {lib.library_id: lib for lib in self.s.scalars(select(Library))}
        file_libs = parts.libraries.libraries if parts.libraries else []
        if not file_libs and self.same_plex:
            # Grants without the libraries part: by id, for libraries this server has.
            self.lib_map = {lid: lid for lid in here_libs}
        elif self.same_plex:
            for lib in file_libs:
                if lib.id in here_libs or not here_libs or not self.here_machine:
                    self.lib_map[lib.id] = lib.id
        else:
            by_title: dict[tuple[str, str], list[str]] = {}
            for lib in here_libs.values():
                by_title.setdefault((lib.title.casefold(), lib.type), []).append(lib.library_id)
            for lib in file_libs:
                match = by_title.get((lib.title.casefold(), lib.type), [])
                if len(match) == 1:
                    self.lib_map[lib.id] = match[0]
        here_roles = {r.role_id: r for r in self.s.scalars(select(DiscordRole).where(DiscordRole.deleted.is_(False)))}
        file_roles: dict[str, str | None] = {}
        if parts.libraries:
            file_roles |= {m.role_id: m.role_name for m in parts.libraries.role_mappings}
        for post in parts.reaction_posts or []:
            file_roles |= {o.role_id: o.label or file_roles.get(o.role_id) for o in post.options}
        if self.same_guild:
            self.role_map = {rid: rid for rid in file_roles}
        else:
            by_name: dict[str, list[str]] = {}
            for role in here_roles.values():
                if not role.managed:
                    by_name.setdefault(role.name.casefold(), []).append(role.role_id)
            for rid, name in file_roles.items():
                match = by_name.get((name or "").casefold(), [])
                if len(match) == 1:
                    self.role_map[rid] = match[0]
        self.file_roles = file_roles
        self.here_roles = here_roles

    def _plex_user(self, plex_user_id: str, username: str | None) -> None:
        if self.apply and self.s.get(PlexUser, plex_user_id) is None:
            # Filled in (or marked removed) by the next Plex inventory read.
            self.s.add(PlexUser(plex_user_id=plex_user_id, username=username or plex_user_id))
            self.s.flush()

    # -- parts --------------------------------------------------------------------------------

    def settings(self) -> None:
        part = self.doc.parts.settings
        assert part is not None
        current = app_settings.load(self.s).model_dump()
        known = set(app_settings.AppSettings.model_fields)
        incoming = {k: v for k, v in part.app.items() if k in known}
        unknown = sorted(set(part.app) - known)
        if unknown:
            self.warn(f"Settings this WooSarr doesn't have were left out: {', '.join(unknown[:8])}.")
        merged = current | incoming
        try:
            app_settings.AppSettings.model_validate(merged)
        except ValidationError:
            merged = dict(current)
            for k, v in incoming.items():
                try:
                    app_settings.AppSettings.model_validate(merged | {k: v})
                    merged[k] = v
                except ValidationError:
                    self.warn(f"Setting {k} from the file isn't valid here and was left as it is.")
        changed = sum(1 for k, v in merged.items() if current.get(k) != v)
        if not self.same_guild:
            self.warn(f"Settings that name roles or channels point at {self.doc.source.discord_guild or 'the other'} "
                      "Discord server; check Settings → Linking and Requests.")
        if self.apply:
            app_settings.update(self.s, merged)
            if part.seerr:
                kv.put(self.s, K_SEERR, part.seerr)
        self.r.add("settings", f"{_plural(changed, 'setting')} would change" if not self.apply
                   else f"{_plural(changed, 'setting')} changed", changed=changed)

    def libraries(self) -> None:
        part = self.doc.parts.libraries
        assert part is not None
        here = {lib.library_id: lib for lib in self.s.scalars(select(Library))}
        added = updated = 0
        missing = []
        for lib in part.libraries:
            target = self.lib_map.get(lib.id)
            if target is None:
                missing.append(lib.title)
                continue
            row = here.get(target)
            if row is None:
                added += 1
                if self.apply:
                    self.s.add(Library(library_id=target, title=lib.title, type=lib.type, is_exempt=lib.exempt,
                                       removed=lib.removed))
            else:
                updated += row.is_exempt != lib.exempt
                if self.apply:
                    row.is_exempt = lib.exempt
        if missing:
            self.warn(f"{_plural(len(missing), 'library')} not found on this Plex server, left out with their role "
                      f"mappings and grants: {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}.")
        if not self.here_machine and part.libraries:
            self.warn("Plex isn't connected here yet: libraries were taken as they were on "
                      f"{self.doc.source.plex_server or 'the other server'}. If this server will use a different Plex "
                      "server, connect Plex first and import again.")
        pairs: set[tuple[str, str]] = set()
        lost_roles = []
        for m in part.role_mappings:
            role = self.role_map.get(m.role_id)
            if role is None:
                lost_roles.append(m.role_name or m.role_id)
                continue
            pairs |= {(role, self.lib_map[lid]) for lid in m.library_ids if lid in self.lib_map}
        if lost_roles:
            self.warn(f"{_plural(len(lost_roles), 'role')} not found on this Discord server, their mappings left out: "
                      f"{', '.join(lost_roles[:8])}.")
        if self.apply:
            self.s.flush()
            self.s.execute(delete(RoleLibrary))
            self.s.add_all([RoleLibrary(role_id=r, library_id=lid) for r, lid in sorted(pairs)])
            self._role_cache()
        self.r.add("libraries", f"{_plural(len(pairs), 'role mapping')} (replacing the current ones); "
                   f"{_plural(added, 'library')} added, {updated} exempt flag(s) changed",
                   mappings=len(pairs), added=added, changed=updated, skipped=len(missing))

    def _role_cache(self) -> None:
        """Names for roles the Discord cache doesn't have yet (not connected): refreshed on connect."""
        if not self.same_guild:
            return
        for rid in set(self.role_map.values()):
            if self.s.get(DiscordRole, rid) is None:
                self.s.add(DiscordRole(role_id=rid, name=self.file_roles.get(rid) or rid))

    def manual_grants(self) -> None:
        rows = self.doc.parts.manual_grants or []
        kept = [(g, self.lib_map[g.library_id]) for g in rows if g.library_id in self.lib_map]
        seen: set[tuple[str, str]] = set()
        if self.apply:
            self.s.execute(delete(ManualGrant))
            for g, lid in kept:
                if (g.plex_user_id, lid) in seen or self.s.get(Library, lid) is None:
                    continue
                seen.add((g.plex_user_id, lid))
                self._plex_user(g.plex_user_id, g.plex_username)
                self.s.add(ManualGrant(plex_user_id=g.plex_user_id, library_id=lid, granted_by=g.granted_by,
                                       granted_at=g.granted_at or utcnow()))
        skipped = len(rows) - len(kept)
        self.r.add("manual_grants", f"{_plural(len(kept), 'grant')} (replacing the current ones)"
                   + (f"; {skipped} for libraries not found" if skipped else ""), grants=len(kept), skipped=skipped)

    def reaction_posts(self) -> None:
        posts = self.doc.parts.reaction_posts or []
        here = list(self.s.scalars(select(ReactionPost)))
        if here and any(p.message_id for p in here):
            self.warn(f"The {_plural(len(here), 'reaction post')} here are replaced; their messages stay in Discord until "
                      "deleted there.")
        unpublished = dropped = 0
        rows = []
        for p in posts:
            options = []
            for o in p.options:
                role = self.role_map.get(o.role_id)
                if role is None:
                    dropped += 1
                    continue
                options.append({**o.model_dump(), "role_id": role})
            keep = self.same_bot and bool(p.message_id)
            unpublished += bool(p.message_id) and not keep
            rows.append(ReactionPost(
                title=p.title, content=p.content, channel_id=p.channel_id if self.same_guild else None,
                message_id=p.message_id if keep else None, pin=p.pin, enabled=p.enabled, options=options,
                legend=p.legend, legend_template=p.legend_template, legend_header=p.legend_header,
                link_button=p.link_button, invite_button=p.invite_button, buttons_posted="",
                published_at=p.published_at if keep else None))
        if unpublished:
            self.warn(f"{_plural(unpublished, 'reaction post')} came from another Discord server or bot and were imported "
                      "unpublished: publish them on the Reactions page.")
        if dropped:
            self.warn(f"{_plural(dropped, 'reaction')} on reaction posts name roles not found here and were left out.")
        if self.apply:
            for old in here:
                self.s.delete(old)
            self.s.flush()
            if any(r.message_id for r in rows):
                taken = {m for m in self.s.scalars(select(ReactionPost.message_id)) if m}
                for row in rows:
                    if row.message_id in taken:
                        row.message_id, row.published_at = None, None
            self.s.add_all(rows)
        self.r.add("reaction_posts", f"{_plural(len(rows), 'reaction post')} (replacing {len(here)})",
                   posts=len(rows), unpublished=unpublished, replaced=len(here))

    def admins(self) -> None:
        rows = self.doc.parts.admins or []
        here = {a.username: a for a in self.s.scalars(select(AdminUser))}
        added = updated = 0
        for a in rows:
            pw = self.values.get(f"admin_password_hash:{a.username}") if SECRETS in self.chosen else None
            if pw is not None and not pw.startswith("$argon2"):
                pw = None
            row = here.get(a.username)
            if row is None:
                added += 1
                if a.auth_source == "local" and not pw:
                    self.warn(f"Admin {a.username} comes without a password: set one under Settings → Admins, or "
                              "import with the passphrase.")
                if self.apply:
                    self.s.add(AdminUser(username=a.username, auth_source=a.auth_source, password_hash=pw,
                                         is_active=a.active, created_at=a.created_at or utcnow()))
            else:
                updated += 1
                if self.apply:
                    if pw:  # the file's password (and sign-in type) replace this server's
                        row.auth_source, row.password_hash, row.failed_logins, row.locked_until = (
                            a.auth_source, pw, 0, None)
                    if a.active or self.actor.removeprefix("admin:") != row.username:
                        row.is_active = a.active
        if self.apply:
            self.s.flush()
            if not self.s.scalar(select(AdminUser.id).where(AdminUser.is_active.is_(True)).limit(1)):
                raise TransferError("the import would leave no active admin")
        self.r.add("admins", f"{_plural(added, 'admin')} added, {updated} updated (none removed)",
                   added=added, updated=updated)

    def sso(self) -> None:
        part = self.doc.parts.sso
        assert part is not None
        here = {p.slug: p for p in self.s.scalars(select(OidcProvider))}
        admins = {a.username: a.id for a in self.s.scalars(select(AdminUser))}
        added = updated = disabled = 0
        for p in part.providers:
            row = here.get(p.slug)
            secret = self.values.get(f"oidc_client_secret:{p.slug}") if SECRETS in self.chosen else None
            has_secret = bool(secret) or (row is not None and self.store.is_set(self.s, names.oidc_client_secret(row.id)))
            enabled = p.enabled and has_secret
            if p.enabled and not has_secret:
                disabled += 1
                self.warn(f"Single sign-on {p.name} was imported switched off: its client secret isn't in the file "
                          "(or no passphrase). Enter it under Settings → Single sign-on.")
            values = p.model_dump() | {"enabled": enabled}
            if row is None:
                added += 1
                if self.apply:
                    row = OidcProvider(**values)
                    self.s.add(row)
                    self.s.flush()
            else:
                updated += 1
                if self.apply:
                    for k, v in values.items():
                        setattr(row, k, v)
            if self.apply and row is not None:
                if secret:
                    self.store.set(self.s, names.oidc_client_secret(row.id), secret)
                self.s.execute(delete(OidcIdentity).where(OidcIdentity.provider_id == row.id))
                for i in part.identities:
                    if i.provider == p.slug and i.admin in admins:
                        self.s.add(OidcIdentity(provider_id=row.id, subject=i.subject, admin_user_id=admins[i.admin],
                                                email=i.email))
        self.r.add("sso", f"{_plural(added, 'provider')} added, {updated} updated", added=added, updated=updated,
                   off=disabled)

    def links(self) -> None:
        rows = self.doc.parts.links or []
        seen: set[str] = set()
        if self.apply:
            self.s.execute(delete(DiscordLink))
        for link in rows:
            if link.discord_user_id in seen:
                continue
            seen.add(link.discord_user_id)
            if self.apply:
                self._plex_user(link.plex_user_id, link.plex_username)
                self.s.add(DiscordLink(plex_user_id=link.plex_user_id, discord_user_id=link.discord_user_id,
                                       method=link.method, verified=link.verified, linked_at=link.linked_at or utcnow(),
                                       linked_by=link.linked_by))
        current = len(list(self.s.scalars(select(DiscordLink.id)))) if not self.apply else None
        self.r.add("links", f"{_plural(len(seen), 'member link')}" + (f" (replacing {current})" if current else ""),
                   links=len(seen))

    def requests(self) -> None:
        rows = self.doc.parts.requests or []
        here = {r.seerr_request_id: r for r in self.s.scalars(select(MediaRequest))}
        added = updated = 0
        for req in rows:
            values = req.model_dump()
            for k in ("created_at", "updated_at", "status_at"):
                values[k] = values[k] or utcnow()
            row = here.get(req.seerr_request_id)
            if row is None:
                added += 1
                if self.apply:
                    row = MediaRequest(**values)
                    self.s.add(row)
                    here[req.seerr_request_id] = row
            else:
                updated += 1
                if self.apply:
                    for k, v in values.items():
                        setattr(row, k, v)
        self.r.add("requests", f"{_plural(added, 'request')} added, {updated} updated", added=added, updated=updated)

    def audit(self) -> None:
        rows = self.doc.parts.audit or []
        here = {(a.ts.isoformat() if a.ts else "", a.actor, a.action, a.target_id or "")
                for a in self.s.scalars(select(AuditLog))}
        new = []
        for a in rows:
            key = (a.ts.isoformat(), a.actor, a.action, a.target_id or "")
            if key in here:
                continue
            here.add(key)
            new.append(a)
        if self.apply:
            self.s.add_all([AuditLog(**a.model_dump()) for a in new])
        self.r.add("audit", f"{_plural(len(new), 'audit entry')} added, {len(rows) - len(new)} already here",
                   added=len(new), skipped=len(rows) - len(new))

    def secrets(self) -> None:
        # Fixed labels and store names; the decrypted values only go to the secret store.
        present = [key for key in TOKEN_NAMES if self.values.get(key)]
        src = self.doc.source
        if self.apply:
            for key in present:
                self.store.set(self.s, TOKEN_NAMES[key], self.values[key])
            # A fresh server learns which Plex server and Discord server the tokens are for.
            if "plex_token" in present:
                self.r.plex_token = True
                if not self.here_machine and src.plex_machine_id:
                    kv.put(self.s, K_PLEX_SERVER, {"machine_id": src.plex_machine_id, "name": src.plex_server})
                if src.plex_account and not kv.get(self.s, K_PLEX_ACCOUNT):
                    kv.put(self.s, K_PLEX_ACCOUNT, src.plex_account)
            if "discord_bot_token" in present:
                self.r.discord_token = True
                if not self.here_guild and src.discord_guild_id:
                    kv.put(self.s, K_DISCORD_GUILD, {"id": src.discord_guild_id, "name": src.discord_guild})
                if src.discord_bot and not kv.get(self.s, K_DISCORD_BOT):
                    kv.put(self.s, K_DISCORD_BOT, src.discord_bot)
        if self.here_machine and not self.same_plex and "plex_token" in present:
            self.warn("The Plex token in the file belongs to another Plex server's owner; importing it replaces this "
                      "server's Plex sign-in.")
        if self.here_guild and not self.same_guild and "discord_bot_token" in present:
            self.warn("The bot token in the file is for a bot on another Discord server; importing it replaces this "
                      "server's bot.")
        others = sum(1 for k in self.values if k not in TOKEN_NAMES)
        self.r.encrypted_count = len(present) + (others if self.apply else 0)
        labels = [label for key, label in SECRET_LABELS.items() if key in present]
        self.r.add(SECRETS, ", ".join(labels) or "no tokens", tokens=len(present))

    def run(self) -> Result:
        # Admins before single sign-on (identities name admins); settings last but one, secrets last.
        order = ["admins", "libraries", "manual_grants", "links", "reaction_posts", "sso", "requests", "audit",
                 "settings", SECRETS]
        for part in order:
            if part in self.chosen:
                getattr(self, part)()
        if self.apply:
            # Dry-run on after any import: nothing changes on Plex until the owner reviews the planned changes.
            app_settings.update(self.s, {"dry_run": True})
        order_index = {p: i for i, p in enumerate(IMPORT_PARTS)}
        self.r.parts.sort(key=lambda pr: order_index[pr.name])
        return self.r


def run(s: Session, store: SecretStore, doc: ExportFile, parts: list[str] | None, passphrase: str | None,
        apply: bool, actor: str = "system") -> Result:
    chosen = list(IMPORT_PARTS) if not parts else parts
    unknown = sorted(set(chosen) - set(IMPORT_PARTS))
    if unknown:
        raise TransferError(f"unknown part(s): {', '.join(unknown)}")
    result = _Run(s, store, doc, chosen, passphrase, apply, actor).run()
    if apply:
        s.flush()
        log.info("config imported by %s from %s / %s: %s; %d secret(s)%s", actor, doc.source.plex_server or "?",
                 doc.source.discord_guild or "?", ", ".join(f"{p.name} {p.counts}" for p in result.parts),
                 result.encrypted_count, f"; {len(result.warnings)} warning(s)" if result.warnings else "")
    return result
