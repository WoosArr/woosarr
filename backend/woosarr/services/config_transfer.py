"""Config export (and, in P15-02, import): a versioned JSON file to move as much or as little of a
WooSarr setup to another server as the owner wants (D23).

Every part is optional. Secrets (Plex/Discord tokens, Seerr key, SSO client secrets, admin password
hashes) are only included on request, encrypted with a passphrase the owner chooses: scrypt derives
the key, AES-GCM encrypts. They never reach the logs or the audit trail.

Never exported: what WooSarr re-reads from Plex and Discord (member/role/Plex-user caches, current
shares), sessions, API keys, invites, logs, alert resolutions and internal bookkeeping.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from woosarr import build
from woosarr.core import app_settings, kv
from woosarr.core import secret_store as names
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretError
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

log = logging.getLogger("woosarr.config_transfer")

FORMAT = 1
KIND = "woosarr-config"
# In the order the UI lists them.
PARTS = ("settings", "libraries", "manual_grants", "reaction_posts", "sso", "links", "admins", "requests", "audit")
PASSPHRASE_MIN = 12
# scrypt cost: ~0.1 s and 32 MB on a small box; stored in the file so it can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
_AAD = b"woosarr-config-secrets-v1"

# Keys in kv the settings part carries (the rest is per-install bookkeeping: Plex client id, the
# connected Plex server and Discord guild, link-help DM times).
K_SEERR = "seerr.config"
K_PLEX_SERVER = "plex.server"
K_DISCORD_GUILD = "discord.guild"


class TransferError(ValueError):
    """Something the owner can fix (a short passphrase, an unknown part); safe to show."""


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---- Parts -------------------------------------------------------------------------------------


def _settings(s: Session) -> dict[str, Any]:
    out: dict[str, Any] = {"app": app_settings.load(s).model_dump(mode="json")}
    seerr = kv.get(s, K_SEERR)
    if seerr:
        out["seerr"] = seerr
    return out


def _libraries(s: Session) -> dict[str, Any]:
    roles = dict(s.execute(select(DiscordRole.role_id, DiscordRole.name)).all())
    mapping: dict[str, list[str]] = {}
    for role_id, library_id in s.execute(select(RoleLibrary.role_id, RoleLibrary.library_id)
                                         .order_by(RoleLibrary.role_id, RoleLibrary.library_id)):
        mapping.setdefault(role_id, []).append(library_id)
    return {
        "libraries": [{"id": lib.library_id, "title": lib.title, "type": lib.type, "exempt": lib.is_exempt,
                       "removed": lib.removed}
                      for lib in s.scalars(select(Library).order_by(Library.title, Library.library_id))],
        "role_mappings": [{"role_id": role_id, "role_name": roles.get(role_id), "library_ids": ids}
                          for role_id, ids in mapping.items()],
    }


def _manual_grants(s: Session) -> list[dict[str, Any]]:
    users = dict(s.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    return [{"plex_user_id": g.plex_user_id, "plex_username": users.get(g.plex_user_id), "library_id": g.library_id,
             "granted_by": g.granted_by, "granted_at": _ts(g.granted_at)}
            for g in s.scalars(select(ManualGrant).order_by(ManualGrant.plex_user_id, ManualGrant.library_id))]


def _reaction_posts(s: Session) -> list[dict[str, Any]]:
    # What the post is and where it was posted; not the posting bookkeeping (rendered text, retired
    # emoji, posted buttons), which the new server works out on its next publish.
    return [{"title": p.title, "content": p.content, "channel_id": p.channel_id, "message_id": p.message_id,
             "pin": p.pin, "enabled": p.enabled, "options": p.options, "legend": p.legend,
             "legend_template": p.legend_template, "legend_header": p.legend_header,
             "link_button": p.link_button, "invite_button": p.invite_button, "published_at": _ts(p.published_at)}
            for p in s.scalars(select(ReactionPost).order_by(ReactionPost.id))]


_PROVIDER_FIELDS = ("slug", "name", "kind", "issuer", "client_id", "scopes", "username_claim", "email_claim",
                    "groups_claim", "allowed_groups", "allowed_emails", "allowed_domains", "require_email_verified",
                    "extra_params", "settings", "enabled")


def _sso(s: Session) -> dict[str, Any]:
    providers = list(s.scalars(select(OidcProvider).order_by(OidcProvider.slug)))
    slugs = {p.id: p.slug for p in providers}
    admins = dict(s.execute(select(AdminUser.id, AdminUser.username)).all())
    return {
        "providers": [{f: getattr(p, f) for f in _PROVIDER_FIELDS} for p in providers],
        # Which admin each provider account signs in as (by subject, never by email).
        "identities": [{"provider": slugs[i.provider_id], "subject": i.subject, "admin": admins.get(i.admin_user_id),
                        "email": i.email}
                       for i in s.scalars(select(OidcIdentity).order_by(OidcIdentity.id)) if i.provider_id in slugs],
    }


def _links(s: Session) -> list[dict[str, Any]]:
    users = dict(s.execute(select(PlexUser.plex_user_id, PlexUser.username)).all())
    return [{"discord_user_id": link.discord_user_id, "plex_user_id": link.plex_user_id,
             "plex_username": users.get(link.plex_user_id), "method": link.method, "verified": link.verified,
             "linked_at": _ts(link.linked_at), "linked_by": link.linked_by}
            for link in s.scalars(select(DiscordLink).order_by(DiscordLink.id))]


def _admins(s: Session) -> list[dict[str, Any]]:
    return [{"username": a.username, "auth_source": a.auth_source, "active": a.is_active,
             "created_at": _ts(a.created_at)}
            for a in s.scalars(select(AdminUser).order_by(AdminUser.id))]


_REQUEST_FIELDS = ("seerr_request_id", "discord_user_id", "plex_user_id", "seerr_user_id", "media_type", "tmdb_id",
                   "title", "year", "seasons", "status", "notified_status", "channel_id", "message_id", "retries")


def _requests(s: Session) -> list[dict[str, Any]]:
    return [{**{f: getattr(r, f) for f in _REQUEST_FIELDS},
             **{f: _ts(getattr(r, f)) for f in ("created_at", "updated_at", "checked_at", "status_at", "retried_at")}}
            for r in s.scalars(select(MediaRequest).order_by(MediaRequest.id))]


def _audit(s: Session) -> list[dict[str, Any]]:
    return [{"ts": _ts(a.ts), "actor": a.actor, "action": a.action, "target_type": a.target_type,
             "target_id": a.target_id, "details": a.details, "dry_run": a.dry_run, "names": a.names}
            for a in s.scalars(select(AuditLog).order_by(AuditLog.id))]


_BUILDERS = {"settings": _settings, "libraries": _libraries, "manual_grants": _manual_grants,
             "reaction_posts": _reaction_posts, "sso": _sso, "links": _links, "admins": _admins,
             "requests": _requests, "audit": _audit}


def part_counts(s: Session) -> dict[str, int]:
    """How many things each part would carry (for the export page)."""
    def count(model: Any) -> int:
        return s.scalar(select(func.count()).select_from(model)) or 0

    return {"settings": 1, "libraries": count(Library), "manual_grants": count(ManualGrant),
            "reaction_posts": count(ReactionPost), "sso": count(OidcProvider), "links": count(DiscordLink),
            "admins": count(AdminUser), "requests": count(MediaRequest), "audit": count(AuditLog)}


# ---- Secrets -----------------------------------------------------------------------------------


def _collect_secrets(s: Session, store: SecretStore, parts: list[str]) -> tuple[dict[str, str], list[str]]:
    """Plain secrets for the encrypted block, and the names that couldn't be read (lost master key)."""
    found: dict[str, str] = {}
    unreadable: list[str] = []

    def take(key: str, name: str) -> None:
        try:
            value = store.get(s, name)
        except SecretError:
            unreadable.append(key)
            return
        if value:
            found[key] = value

    take("plex_token", names.PLEX_TOKEN)
    take("discord_bot_token", names.DISCORD_BOT_TOKEN)
    take("seerr_api_key", names.SEERR_API_KEY)
    if "sso" in parts:
        # By slug: provider ids differ between servers.
        for pid, slug in s.execute(select(OidcProvider.id, OidcProvider.slug)):
            take(f"oidc_client_secret:{slug}", names.oidc_client_secret(pid))
    if "admins" in parts:
        for username, password_hash in s.execute(select(AdminUser.username, AdminUser.password_hash)):
            if password_hash:
                found[f"admin_password_hash:{username}"] = password_hash
    return found, unreadable


def _kdf(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def check_passphrase(passphrase: str | None) -> str:
    if not passphrase or len(passphrase) < PASSPHRASE_MIN:
        raise TransferError(f"the passphrase must be at least {PASSPHRASE_MIN} characters")
    return passphrase


def encrypt_secrets(values: dict[str, str], passphrase: str) -> dict[str, Any]:
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _kdf(check_passphrase(passphrase), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    blob = AESGCM(key).encrypt(nonce, json.dumps(values, sort_keys=True).encode(), _AAD)
    b64 = lambda raw: base64.b64encode(raw).decode()
    return {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": b64(salt),
            "cipher": "aes-256-gcm", "nonce": b64(nonce), "ciphertext": b64(blob), "count": len(values)}


def decrypt_secrets(block: dict[str, Any], passphrase: str) -> dict[str, str]:
    """The secrets in an export; TransferError for a wrong passphrase or a damaged block."""
    try:
        if block.get("kdf") != "scrypt" or block.get("cipher") != "aes-256-gcm":
            raise TransferError("the secrets use an unknown encryption")
        n, r, p = int(block["n"]), int(block["r"]), int(block["p"])
        if not (2**14 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
            raise TransferError("the secrets use unsupported encryption settings")
        key = _kdf(passphrase, base64.b64decode(block["salt"]), n, r, p)
        plain = AESGCM(key).decrypt(base64.b64decode(block["nonce"]), base64.b64decode(block["ciphertext"]), _AAD)
    except InvalidTag:
        raise TransferError("wrong passphrase, or the secrets in the file are damaged") from None
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, TransferError):
            raise
        raise TransferError("the secrets in the file are damaged") from None
    values = json.loads(plain)
    if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
        raise TransferError("the secrets in the file are damaged")
    return values


# ---- Export ------------------------------------------------------------------------------------


def normalize_parts(parts: list[str] | None) -> list[str]:
    if not parts:
        return list(PARTS)
    unknown = sorted(set(parts) - set(PARTS))
    if unknown:
        raise TransferError(f"unknown part(s): {', '.join(unknown)}; choose from {', '.join(PARTS)}")
    return [p for p in PARTS if p in parts]


def export(s: Session, store: SecretStore, parts: list[str] | None = None, passphrase: str | None = None,
           include_secrets: bool = False) -> dict[str, Any]:
    """The export file as a dict. include_secrets needs a passphrase (PASSPHRASE_MIN characters)."""
    chosen = normalize_parts(parts)
    if include_secrets:
        check_passphrase(passphrase)
    server = kv.get(s, K_PLEX_SERVER) or {}
    guild = kv.get(s, K_DISCORD_GUILD) or {}
    doc: dict[str, Any] = {
        "kind": KIND, "format": FORMAT, "woosarr": build.current().version,
        "exported_at": utcnow().isoformat(),
        "source": {"plex_machine_id": server.get("machine_id"), "plex_server": server.get("name"),
                   "discord_guild_id": guild.get("id"), "discord_guild": guild.get("name")},
        "parts": {p: _BUILDERS[p](s) for p in chosen},
        "secrets": None,
    }
    counts = {p: (len(v) if isinstance(v, list) else None) for p, v in doc["parts"].items()}
    # Only how many values were encrypted and which names couldn't be read go to the log and audit.
    encrypted, unreadable = 0, []
    if include_secrets:
        values, unreadable = _collect_secrets(s, store, chosen)
        encrypted = len(values)
        doc["secrets"] = encrypt_secrets(values, passphrase or "")
        if unreadable:
            doc["unreadable"] = unreadable  # names only: couldn't be decrypted with this server's key
    log.info("config exported: parts=%s encrypted values=%d%s", ",".join(chosen), encrypted,
             f" (unreadable: {', '.join(unreadable)})" if unreadable else "")
    doc["summary"] = {"parts": chosen, "counts": {k: v for k, v in counts.items() if v is not None},
                      "encrypted": encrypted, "unreadable": unreadable}
    return doc


def filename(when: datetime | None = None) -> str:
    return f"woosarr-config-{(when or utcnow()).strftime('%Y-%m-%d')}.json"
