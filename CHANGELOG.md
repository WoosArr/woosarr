# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

## [Unreleased]

### Added
- Customizable dashboard: widgets can be moved, resized and hidden per admin, with new widgets for link coverage, access changes per day, recent activity, library access, requests, Plex invites, a setup checklist and security (failed sign-ins, sessions, API keys), plus individual number tiles.
- Warnings and errors on the dashboard are grouped by problem and can be **marked resolved**, which hides them from the dashboard for 24 hours (audited; Unresolve available).
- Users page: **Linked** tab (verified + unverified) between Unverified and Unlinked.

### Fixed
- Publishing a reaction post into a channel where the bot lacks Add Reactions / Read Message History / Use External Emojis failed with an internal error after the message was already sent; the permissions are now checked first and Discord refusals are shown with the missing permission.
- `/link` → *Sign in with Plex* could fail with "Unknown interaction" because WooSarr contacted plex.tv before answering Discord's 3-second deadline.
- Routine Discord gateway reconnects (resumed within a second) were logged as warnings; they're INFO now, with a warning only if the bot stays disconnected for 60 s.
- A reset or revoked bot token (gateway close 4004) now shows "Discord rejected the bot token" instead of a raw error.
- Seerr requests deleted in Seerr no longer log a warning on every status check.
- Warnings and errors could drop out of the in-memory log buffer before the dashboard counted them.

### Added
- Setup → Discord → **Bot avatar**: set the bot's avatar to the WooSarr icon (default) or an uploaded PNG/JPEG/GIF (checked by its bytes; at least 128×128, up to 8 MB), with a circular preview, and optionally use it as the application icon. Uses the bot's own token; Discord's limit of about two changes per hour is reported with the wait time. Audited.

### Changed
- New WooSarr icon (raven over a purple moon) everywhere: browser tab (SVG + ICO), iOS/Android home screen and web manifest, sidebar and sign-in screen, API docs page, Unraid template, README; a 512 px PNG to use as the Discord bot's avatar is linked from the Setup page. All sizes are rendered from `assets/brand/woosarr.svg` with `make icons`.

### Security
- Discord messages escape titles, overviews and Seerr messages fully (TMDB data could otherwise inject masked links, e.g. `](https://…) [`, into the pick list, public request posts and status replies); URL parts from Seerr (poster path, IMDb/TVDB ids, year) are validated.
- `/request` suggestions only search for members who may request (and in the right channel); per-member search limit (30 per minute).
- SSO return path refuses control characters (`/\t/host` would become `//host` in browsers) and very long values; first-run setup code input capped at 64 characters.
- Container: `umask 027`, so the database and logs aren't readable by other host users; warning when `PUID=0`.
- Frontend toolchain upgraded to vite 8.3.4, vitest 5.0.5 and @vitejs/plugin-react 6.1.2 (tinypool RCE and vite/esbuild dev-server advisories; dev only, nothing in the image was affected). `npm audit` is clean including dev dependencies.

### Added
- `/request` suggestions and menus mark movies with 🍿 (was 🎬, too close to 📺 for shows); long titles are shortened so the "available" / "partly available" / "requested" note always stays visible.
- Requests can be **public**: each request is posted in an admin-chosen channel mentioning the member, and status changes reply to that post and update its status (private requests keep DMs). Optional "only in this channel" for `/request`. New bot feature *Public requests* (View Channels, Send Messages, Embed Links, Read Message History in that channel).
- `/request`: the "Which one?" list links each title to TMDB (choosing stays in the menu), and the details card links to TMDB, TVDB (shows) and IMDb.
- Requests through Seerr (Overseerr/Jellyseerr compatible): members search and request movies and TV shows from Discord with `/request` (suggestions as they type, poster card, season picker), made as their own Seerr user so Seerr's permissions, quotas and auto-approval apply; DM when approved, declined or available; Setup card with connection test, Settings → Requests (command name, roles, verified links only, create missing Seerr users, notifications), Requests page. The command's name can be changed when another bot already has `/request`. See docs/requests.md.
- Admin single sign-on with OpenID Connect: multiple providers, presets for Okta, Microsoft Entra ID, Google and ADFS plus Custom; setup wizard with requirements and step-by-step instructions; test sign-in showing the claims and decision; access rules by group/role, email or domain; admins created on first sign-in and linked by subject; password login can be turned off once SSO works (lockout guard). See docs/sso.md.

### Changed
- Reaction posts list each emoji's role and libraries under the message (editable line template, heading and per-reaction lines, live preview with length check); posts are edited automatically when a role's libraries, a library name or a role name changes.
- Remove libraries: each library shows ✓ / – / empty for whether all, some or none of the selected users have it; untick to remove; live impact preview, Reset, and Undo after removing.
- Settings that change access (dry-run, leave-server policy, ignore unverified links) show who gains/loses what before saving; the go-live confirmation includes the numbers.
- Login rate limiting counts only failed attempts per IP (successful sign-ins no longer count towards the limit).
- Reaction post options: one role per reaction, picked from a dropdown (with inline *Create new role*); the label is always the role's name.

### Added
- Reaction post editor: emoji picker with search (this server's custom emoji + common emoji); typing/pasting still works.
- Roles page: create a Discord role (name, colour, optional libraries).
- Users page: **Remove libraries** removes the selected libraries from users in Plex now (and their manual grants); libraries a linked user's roles grant are kept and reported. Replaces "Remove manual grants", which only removed WooSarr manual grants and did nothing for shares made in Plex or by roles before unlinking.
- Link-status roles: the bot keeps optional Verified / Unverified / Linked roles in step with each member's Plex link; roles can be created from Settings.
- Libraries page: create a Discord role for a library (mapped automatically).
- `/invite`: "All members" option.
- Reaction posts (new Reactions page): post and pin a message in a chosen or newly created channel; reacting gives Discord roles and removing the reaction takes them away (libraries follow from the role mappings); roles re-synced from the reactions on every reconnect.
- Bot permissions per feature: live availability in Setup, features disabled when permissions are missing, invite link built for the chosen features; documented in `docs/discord-notes.md`.
- `/unlink` Discord command with a confirmation; new setting *When a member unlinks themselves* (remove role-granted libraries, or leave access as it is).
- `/invite email [member]` Discord command: invites a Plex account with the libraries the member's roles grant; members with an allowed role can invite themselves, Discord admins can invite for anyone; accepted invites are linked automatically; pending invites shown on the Users page.
- Libraries page shows the Plex server's name, version, platform and online state.
- Playwright tests for the Plex and Discord setup wizards; simulated Discord event tests.

### Fixed
- A first retry after plex.tv drops an idle connection is logged as INFO, not WARNING; `charset_normalizer` debug noise is capped.
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
