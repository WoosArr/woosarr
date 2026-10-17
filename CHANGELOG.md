# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

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
