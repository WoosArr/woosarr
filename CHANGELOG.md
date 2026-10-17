# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

## [Unreleased]

### Added
- Libraries page shows the Plex server's name, version, platform and online state.
- Playwright tests for the Plex and Discord setup wizards; simulated Discord event tests.

### Fixed
- Re-keying a friend from `uuid:` to their numeric id now merges into an existing (e.g. previously removed) row, keeping link and manual grants; leftover duplicates clean themselves up on the next full sync.
- `plex.tv/api/v2/friends` is gone (410): friends now come from `clients.plex.tv` or Plex's community API; friends known only by username are shared with by username and re-keyed once Plex reports their id.
- Discord interaction and webhook tokens are redacted from logs, and discord.py's own logging stays at INFO when the app log level is DEBUG.
- An unavailable optional Plex source is a single warning, not an error on every sync.
- Friends without any shared library (and users whose last library was removed) are now listed and can be managed: plex.tv's `/api/users` leaves them out, so WooSarr also reads `/api/v2/friends` and Home users.
- The server owner can `/link`; the owner is shown with an Owner badge and is never synced.
- "Verify sharing" works on a user with no libraries (adds one, then removes the share again).
- "Verify sharing" dialog waits for the first user sync instead of showing an empty list.

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
- Full reconciles read every user's shares in one batch (63 users: ~36 s → a few seconds).
