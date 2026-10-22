# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

## [Unreleased]

### Added
- **Custom Emoji page.** Search **emoji.gg**, **OpenMoji** and **slackmojis** and add emoji to your Discord server in a click; rename and delete the server's emoji on the same page. Emoji you add show up straight away in the reaction post picker. *Hide animated* (on by default) leaves out animated emoji, which members can only use with Discord Nitro. WooSarr fetches the previews itself (your browser never contacts those sites), downloads the emoji.gg and OpenMoji lists at startup and once a day (NSFW emoji excluded), and resizes images to fit Discord (128 px, 256 KB, animations kept). Renaming an emoji that a reaction post uses updates the post; deleting one that a post uses is refused. The bot needs the **Create Expressions** and **Manage Expressions** permissions ([Discord notes](docs/discord-notes.md#bot-permissions-by-feature)).

- **Changing a reaction post's emoji keeps members' roles.** Reactions with the old emoji keep giving the role (the bot takes its own old reaction off so new members use the new one), or turn off *Keep old reactions working* to have the old reactions removed when you update the post (needs **Manage Messages**). Before saving, the editor shows who reacted with each emoji that's going away, who'd lose a role, and the Plex impact.

### Fixed
- Members who had reacted with an emoji that was then changed or removed from a reaction post kept that role for good, even after taking their reaction off. Now the role is removed when the post is updated (unless the old reaction is kept working).
- The reaction post emoji picker shows emoji added to the server since WooSarr started (it fetched the list once per session, and the bot didn't receive emoji updates from Discord). It now also has a *Hide animated* switch.

## [1.1.1] - 2026-10-21

Security and reliability update: no vulnerable copies of certifi and friends left in the image, WooSarr can no longer freeze on a slow disk, alerts that come back after you resolve them show again, and current runtimes (Python 3.14, Node 24, React 19). Update by pulling `latest` (or `1.1.1`).

### Security
- **pip removed from the image.** It's only needed to build the image, and its bundled copies of certifi (the compromised-CA issue), urllib3, msgpack and pkg_resources showed up as 1 critical and 4 high vulnerabilities, even though WooSarr never runs them. The image scan now fails the build on any fixable high or critical vulnerability instead of only reporting it.
- gitleaks 8.30.1 scans the whole history with no blanket exceptions: the whole-file allowlist for the redaction tests is gone (their fake password is now assembled at runtime), and two findings in old commits that aren't secrets are ignored by exact fingerprint (`.gitleaksignore`).

### Fixed
- **Resolve** on an alert hid new occurrences of the same problem for 24 hours. It now hides only what had happened when you resolved it; if the problem happens again, the alert is back right away. Alerts resolved before this update follow the new rule too.
- A slow disk could freeze WooSarr: log lines were written to `/config/logs` from the main event loop, and one write that took over 10 s (live, 2026-10-21) stalled the Discord connection and every web request ("heartbeat blocked for more than 10 seconds"). Log files and container output are now written by a background thread; the Logs page and Alerts stay immediate.
- Version tags also moved the `latest` image tag (a later hotfix tag for an older version would have moved it backwards); `latest` now comes only from `main`.

### Changed
- Runtime **Python 3.14** (was 3.12; the image tracks the newest 3.14 patch) and **Node 24 LTS** for the UI build (was 22). Python 3.15 waits until every dependency ships ready-built packages for it (PyYAML doesn't yet).
- **React 19** (was 18); FastAPI 0.159, SQLAlchemy 2.1.6, Starlette 1.9, websockets 17.3, pydantic-settings 2.16.1, Vite 8.3.5, Playwright 1.64.1. TypeScript stays on 5.9 (the API type generator doesn't support TypeScript 7 yet).
- Build tooling: every GitHub Action to its newest release (pinned by commit), gitleaks 8.30.1, Trivy 0.76.1; pre-commit hooks pinned by commit.

## [1.1.0] - 2026-10-21

Users page with a Discord tab and bulk role changes, clearer dashboard and audit trail, a certificate security fix, and a hardened container. Update by pulling `latest` (or `1.1.0`).

### Security
- **certifi 2026.10.19** (was 2026.7.22): drops the `emSign ECC Root CA - G3` root, whose eMudhra intermediates had compromised keys and could be used to impersonate any HTTPS site WooSarr connects to (plex.tv, Discord, Seerr). PYSEC-2026-4320.
- Container hardening: the Unraid template and README run WooSarr with `no-new-privileges` (re-create the container from the template to pick it up); `compose.yaml` also drops every Linux capability except the four the entrypoint needs.
- Build pipeline: GitHub Actions pinned to commit SHAs and scanner images to digests, no tokens left in checkouts, default-deny token permissions, job time limits; Dependabot alerts on; `main` protected (pull request and passing checks required).

### Added
- **Users page: Plex and Discord tabs.** The Discord tab lists the server's members with their roles and linked Plex account (All / Verified / Unverified / Linked / Unlinked, search), with **Add roles** / **Remove roles** for selected members and a preview of which Plex libraries linked accounts would gain or lose. New bot feature *Give and remove roles* (Manage Roles). Audited.
- Dashboard *Requests* widget shows who asked (their Plex username).
- Every audited action has a readable name on the audit trail (before, about 50 showed raw codes such as `reaction_post_created`).
- `ghcr.io/woosarr/woosarr:develop` images of the development branch, for testing (never `latest`).

### Changed
- "Last sync" and audit reasons use names instead of Discord ids ("role Movie Night mapping changed", "exemption changed for Family Videos", "discord roles changed for Alice").
- Discord gateway lag under 60 s ("Can't keep up … behind", which discord.py recovers from by itself) is logged as INFO instead of raising an Alert; longer lag is still a warning.
- Image builds and CI are faster (about 3 minutes instead of 6–8): dependency layers are cached per platform, and the image is built and smoke-tested alongside the tests.
- Development happens on the `develop` branch (pull requests go there). `main` stays the default branch and is the release branch.

### Fixed
- Bots and integrations were listed as Discord members: selecting everyone on Users → Discord reported them as failures, and they counted as members who haven't linked. They're now marked in the member cache (filled in on the bot's next connect) and left out of member lists and counts.
- The `latest` image tag is tied to `main` explicitly, so it keeps following releases whichever branch is GitHub's default.

## [1.0.0] - 2026-10-19

First stable release. Highlights since 0.1.0: shared Plex accounts (families), Alerts with account events, the Discord bot's `/unlink`, `/invite`, `/help` and reaction posts, requests through Seerr, admin single sign-on, a customizable dashboard, Docker Compose support, and the move to the AGPL-3.0 licence.

### Added
- `compose.yaml`: run WooSarr with Docker Compose (data folder, port, `PUID`/`PGID` and log level via `.env`; Unraid WebUI/icon labels for Compose stacks). CI starts the built image through it before publishing.
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
