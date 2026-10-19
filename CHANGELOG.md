# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

## [Unreleased]

### Added
- **Shared Plex accounts (families):** several Discord members can be linked to one Plex account, which gets the libraries all their roles give. Admins add members on the Users page (**Add member**); *Settings → Linking → Shared Plex accounts* can also let members join by signing in with that Plex account (typing a username never joins). Leaving or unlinking follows the existing policies per member; under "leave as it is", a departing member's libraries become manual grants.
- **Alerts** (was *Warnings and errors*): no 24-hour limit, and **Account** alerts from the audit log naming everyone involved: attempts to link an account someone else holds, takeovers of unverified links by Plex sign-in, members joining shared accounts, and admins adding members.
- Warnings and errors are also written to `/config/logs/alerts.log` (2 MB × 3) and reloaded at startup, so alerts survive restarts; `GET /api/v1/logs/file?name=alerts` downloads it.
- *Settings → Linking → Who may link by typing a Plex username*: limit unverified linking to chosen roles; everyone else only sees *Sign in with Plex*.
- `/help` Discord command: a private list of the bot's commands and their options.
- Dashboard widgets: **Discord role members** (members per role, linked members, roles that give no libraries) and **Invites accepted this week**.
- Settings: **Linking** tab (username linking, shared accounts, cooldown, leave/unlink policies, link-status roles, Plex Invites); logging settings moved into **General**. Old `?tab=invites` / `?tab=logging` links still work.
- `GET /api/v1/discord/status` reports `connecting` while the bot is starting.
- Roles page: **Remove** roles that were deleted in Discord (one, or all at once). Cleans up their library mappings, Settings references (link-status roles, invite/request/username-link role lists) and reaction-post options, after a confirmation listing exactly what goes. Audited.
- Libraries page: **Create role** lets the admin set the role's name (prefilled `Plex · <library>`), colour and extra libraries.
- Customizable dashboard: widgets can be moved, resized (eighth, quarter, half, full width) and hidden per admin, with **Circle** or **Bars** views for chartable widgets; widgets for link coverage, access changes per day, recent activity, library access, requests, Plex invites, a setup checklist and security, plus number tiles.
- Alerts can be **marked resolved** (audited; Unresolve available).
- Users page: **Linked** tab (verified + unverified) between Unverified and Unlinked.
- Integrations → Discord → **Bot avatar**: set the bot's avatar to the WooSarr icon (default) or an uploaded PNG/JPEG/GIF (checked by its bytes; at least 128×128, up to 8 MB), with a circular preview, and optionally use it as the application icon. Audited.
- Requests through Seerr (Overseerr/Jellyseerr compatible): `/request` with suggestions, poster card and season picker, made as the member's own Seerr user; DMs or **public** posts in a requests channel for status changes; Settings → Requests; Requests page. See docs/requests.md.
- Admin single sign-on with OpenID Connect (Okta, Microsoft Entra ID, Google, ADFS, Custom), with a setup wizard, test sign-in, access rules and a lockout guard. See docs/sso.md.
- Reaction posts: post (and pin) a message where reacting gives Discord roles; legend lines listing each role's libraries, kept up to date automatically; emoji picker.
- Link-status roles (Verified / Unverified / Linked), per-library roles, creating roles from the Roles page.
- `/unlink` (with a confirmation and a policy setting) and `/invite email [member]` (Plex invites with the member's role libraries; accepted invites link automatically).
- Users page: **Remove libraries** (tri-state boxes, impact preview, Undo).
- Bot permissions per feature: live availability on the Integrations page, features disabled when permissions are missing, invite link built for the chosen features.
- Documentation screenshots (`make screenshots`, demo data only) in the README and admin guide.

### Changed
- **Licence: WooSarr is now licensed under the GNU AGPL v3.0** (AGPL-3.0-only). Earlier versions, including v0.1.0, remain available under MIT. The web UI links to the source code.
- Sidebar: *Reactions* is now **Reaction Posts** and *Setup* is now **Integrations**.
- An admin linking a Discord member to an already-linked Plex account now **adds** them (shared account) instead of replacing the existing member; unlinking names the member to remove.
- The dashboard counts the Plex server owner as a user everywhere (Link coverage, Library access), and a shared account once.
- User, member, library, admin and SSO provider lists sort case-insensitively.
- Pending invites tile shows just the number; the Alerts empty state is one compact line.
- Settings cards put each setting in its own band with a divider.
- The reaction post editor closes after *Save draft*, *Save & post* and *Save & update post*; if Discord refuses the post, a toast says why (the post is saved).
- New WooSarr icon (raven over a purple moon) everywhere, rendered from `assets/brand/woosarr.svg` with `make icons`.
- Settings that change access show who gains/loses what before saving; the go-live confirmation includes the numbers.
- Login rate limiting counts only failed attempts per IP.

### Fixed
- Pages that check the bot's permissions briefly showed "Discord isn't connected" while loading; they now say *Checking the Discord connection…* or *Connecting to Discord…*.
- The Seerr address and request command name fields were unstyled.
- Removing a role deleted in Discord now also removes it from the username-linking role list.
- Discord role and member cache updates could fail with "UNIQUE constraint failed" when Discord sent two events at once; writes are now atomic upserts.
- Publishing a reaction post where the bot lacks channel permissions failed with an internal error after the message was sent; permissions are checked first and the missing one is named.
- `/link` → *Sign in with Plex* could fail with "Unknown interaction" (Discord's 3-second deadline).
- Routine Discord gateway reconnects are INFO; a warning only if the bot stays disconnected for 60 s. A reset bot token shows "Discord rejected the bot token".
- Seerr requests deleted in Seerr no longer log a warning on every status check.
- Friends without a shared library are listed and can be managed (`plex.tv/api/v2/friends` is gone; friends come from `clients.plex.tv` or the community API and are re-keyed once Plex reports their id).
- Discord interaction and webhook tokens are redacted from logs.

### Security
- Discord messages fully escape TMDB titles and overviews (masked-link injection); URL parts from Seerr are validated.
- `/request` suggestions only search for members who may request; per-member search limit.
- SSO return path refuses control characters and very long values; setup code input capped.
- Container: `umask 027`; warning when `PUID=0`.
- Frontend toolchain upgraded (vite 8, vitest 5); `npm audit` is clean including dev dependencies.

## [0.1.0] - 2026-10-17

First usable release. WooSarr starts in **dry-run**.

### Added
- Sync engine: additive role grants, exemptions, manual grants, unmanaged libraries left alone, complete-set writes with a drift check, leave-server policy, debounced event syncs and a periodic reconcile.
- Plex: OAuth sign-in in the web UI, owned-server picker, plex.tv-only gateway (no LAN connection needed), read check and a write check that verifies share behaviour on a test user and restores it.
- Discord: bot token entered and validated in the web UI (incl. Server Members Intent), server picker, role/member cache, event-driven syncs, `/link` (verified Plex sign-in or username).
- Web UI: dashboard, users, roles with impact preview, libraries with exemptions, logs + audit trail, settings, setup wizards; light/dark, colour-vision palettes, extra contrast; WCAG AA contrast and axe checks.
- Security: first-run setup code, argon2id admin login, CSRF, lockout and rate limits, read/admin API keys, encrypted secrets, log redaction, CSP and security headers, console recovery (`python -m woosarr.admin`).
- Operations: logs API for remote troubleshooting, `linux/amd64` + `linux/arm64` images, Unraid template, smoke-tested and secret-scanned images.

### Fixed during live testing
- Full reconciles read every user's shares in one batch (about 60 users: ~36 s → a few seconds).
