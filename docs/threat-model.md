# WooSarr threat model (P10-06)

WooSarr holds a Plex server-owner token and a Discord bot token, and it can change which Plex libraries people see. This page lists what we protect, from whom, and how.

## Assets

| Asset | Where it lives | Impact if stolen or misused |
|-------|----------------|-----------------------------|
| Plex owner token | `secrets` table, Fernet-encrypted | Full control of the Plex account: shares, libraries, friends |
| Discord bot token | `secrets` table, Fernet-encrypted | Impersonate the bot in the guild (bounded by the bot's permissions) |
| Master key | `/config/secrets/master.key` (0600) or `WOOSARR_MASTER_KEY[_FILE]` | Decrypts the two tokens above |
| Admin passwords | `admin_users.password_hash` (argon2id) | Offline guessing only |
| Sessions / API keys | `sessions.id_hash`, `api_keys.key_hash` (sha256 of 256-bit random values) | Useless without the raw value |
| Library access decisions | role map, manual grants, exemptions, links | Users gain or lose libraries |

## Trust boundaries

1. **Browser ↔ WooSarr** (port 31337, plain HTTP unless a reverse proxy adds TLS).
2. **Discord members ↔ WooSarr** via the bot (`/link`).
3. **WooSarr ↔ plex.tv / the Plex server** and **WooSarr ↔ Discord** (outbound HTTPS).
4. **Host ↔ container** (`/config` volume, env vars, container logs).

## Threats and mitigations

### Unverified `/link` claims (accepted risk)
A Discord member types someone else's Plex username and gets that person's role-based access applied to *the Plex account*. They don't gain access themselves; the real Plex user does. The risk is that an attacker can *add or remove* libraries on someone else's account through role changes.
- One Plex account ↔ one Discord account; first come, first served (`linking.decide_link`).
- A verified link (Plex sign-in or admin) supersedes an unverified claim.
- The UI shows **Verified/Unverified** with icon and text; admins can unlink anyone.
- `ignore_unverified_links` setting manages only verified users (R4).
- Per-user `/link` cooldown; every link event is audited.

### Role escalation in Discord
Anyone who can assign roles in the guild effectively grants Plex libraries. WooSarr can't stop that; it's how the product works. Mitigations: the Roles page shows which roles grant what; the audit log records every access change with its trigger; exempt libraries are never touched by roles.

### `/config` theft (backup leak, compromised host)
With the DB *and* the master key, an attacker decrypts both tokens.
- Keep the key outside `/config` with `WOOSARR_MASTER_KEY_FILE` (e.g. a Docker secret) so a DB backup alone is useless.
- Tokens are never logged, never returned by the API, never shown in the UI (masked "set" indicator only).
- Revoke the Plex token at plex.tv → *Authorized Devices* and reset the bot token in the Discord developer portal if `/config` leaks.

### Network attackers on the LAN
Port 31337 is plain HTTP: anyone on the path can read session cookies.
- Put WooSarr behind an HTTPS reverse proxy for anything beyond a trusted LAN (README → *HTTPS*). Set `WOOSARR_FORWARDED_ALLOW_IPS` to the proxy IP so cookies get `Secure` automatically, or force `WOOSARR_COOKIE_SECURE=always`.
- Cookies are `HttpOnly`, `SameSite=Lax`; unsafe methods need the per-session `X-CSRF-Token` header.
- CSP `default-src 'self'`, `frame-ancestors 'none'`, `nosniff`, no CORS.

### First-run takeover
Before an admin exists, whoever reaches the UI first could create one.
- Creating the first admin requires a one-time **setup code** printed only to the container's stdout (not the log file or logs API). Codes are 48-bit, compared in constant time, and attempts are rate limited per IP.
- The claim is atomic (process lock + unique DB row): a second concurrent claim fails.
- The setup endpoint is gone forever once an admin exists.

### Password guessing
argon2id (argon2-cffi defaults: t=3, m=64 MiB, p=4), 12-character minimum, per-account lockout (5 failures → 15 min), per-IP limiter (10 failed attempts / 5 min; successful sign-ins don't count, so several devices behind one address aren't locked out), identical errors for unknown users and wrong passwords, dummy hash verification to equalise timing.

### Session theft / fixation
Session ids are rotated on every login; logout deletes the server-side row; idle (12 h) and absolute (7 d) expiry; password change or admin disable ends other sessions.

### API key leakage
Keys are shown once, stored as sha256, revocable, and scoped: `read` keys can only use GET/HEAD. Keys can't mint keys. Key-shaped strings are redacted from logs.

### Secrets in logs
`RedactingFilter` on every handler (stderr, file, in-memory buffer) scrubs registered secret values plus token patterns (Plex `X-Plex-Token`, `?token=`, `Authorization: Bearer/Bot`, Discord token shape, `wsk_` keys) in messages and tracebacks. Library loggers (`uvicorn`, `plexapi`, `discord`, `urllib3`, `aiohttp`, `httpx`) go through the same handlers. Covered by `tests/test_redaction_audit.py`.

### SAML forgery / replay (P6-05, not built yet)
Will require signed assertions, audience and recipient checks, clock-skew limits, one-time assertion ids, and a lockout guard before SAML-only mode is enabled.

### Plex share mistakes (integrity)
Plex replaces a user's whole share list on update. WooSarr always computes and writes the **complete** set, re-reads live state right before writing and skips on drift, never touches exempt or unmanaged libraries, and starts in **dry-run**.

### Supply chain
Pinned Python versions (`backend/constraints.txt`), npm lockfile, `pip-audit` and `npm audit --omit=dev` in CI, gitleaks on the repo history, Trivy secret scan of the image before publishing. Known accepted: `npm audit` dev-only findings for the Vite dev server (not in the image).

## Out of scope

A malicious Plex server owner or Discord guild owner; host root compromise (they can read the key wherever it lives); denial of service.
