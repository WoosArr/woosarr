# Changelog

All notable changes. Versions follow [semver](https://semver.org); images are published as `ghcr.io/woosarr/woosarr:<version>`.

## [Unreleased]

### Added
- **TrueNAS and Proxmox VE install guides.** TrueNAS (24.10+): *Install via YAML* with [`truenas/compose.yaml`](truenas/compose.yaml), running as TrueNAS's apps user (568) with no capabilities ([TrueNAS notes](docs/truenas-notes.md)); an app for the TrueNAS Apps catalog is prepared. Proxmox VE: Docker Compose in a Debian LXC or a VM, or the image straight as an LXC on 9.1+ ([Proxmox notes](docs/proxmox-notes.md)). Every image build is now also started as a non-root user, as TrueNAS does.
- **Custom Emoji page.** Search **emoji.gg**, **OpenMoji** and **slackmojis** and add emoji to your Discord server in a click; rename and delete the server's emoji on the same page. Emoji you add show up straight away in the reaction post picker. *Hide animated* (on by default) leaves out animated emoji, which members can only use with Discord Nitro. WooSarr fetches the previews itself (your browser never contacts those sites), downloads the emoji.gg and OpenMoji lists at startup and once a day (NSFW emoji excluded), and resizes images to fit Discord (128 px, 256 KB, animations kept). Renaming an emoji that a reaction post uses updates the post; deleting one that a post uses is refused. The bot needs the **Create Expressions** and **Manage Expressions** permissions ([Discord notes](docs/discord-notes.md#bot-permissions-by-feature)).
- **Changing a reaction post's emoji keeps members' roles.** Reactions with the old emoji keep giving the role (the bot takes its own old reaction off so new members use the new one), or turn off *Keep old reactions working* to have the old reactions removed when you update the post (needs **Manage Messages**). Before saving, the editor shows who reacted with each emoji that's going away, who'd lose a role, and the Plex impact.
- **Which build am I on?** Releases read `1.2.0`; `develop` builds read `1.3.0-dev.14` (commits since the last release, so higher is newer), with the commit and date, under the dashboard title, in the sidebar, in `/api/health` and the startup log. WooSarr checks GitHub at startup and every 6 hours for a newer build of its own channel (releases: newest release; develop builds: newest develop build); turn it off or choose how often (6 h by default, down to 5 min) in *Settings → General*.
- Dashboard **Time drift** tile: how many seconds the server's clock is off from Discord's, green under 30 s, yellow 30-60 s, red over 60 s.
- **Discord bot status**: the bot shows "Try /help for help" under its name in Discord; edit it (or clear it) in *Settings → General*.
- Dashboard **Discord users** and **Linked Discord users** tiles (members and how many are linked; linked members and how many are verified), as a number, a circle or bars. The linked tile opens Users → Discord filtered to linked members.
- Dashboard widgets can be **renamed** and **added more than once** (each copy with its own name, size and chart), in *Customize*. Copies of *Library access*, *Discord role members*, *Roles that grant libraries*, *Requests* and *Alerts* can be **filtered** (libraries, roles, statuses, alert kinds); a filtered widget says so next to its name.
- Dashboard **Custom emoji** and **Animated emoji** tiles: the server's emoji slots used / maximum (e.g. `12/50`), as a number, a circle or bars.
- Dashboard **Blank space** widget for spacing: add as many as you like, size them like other widgets.
- Dashboard widgets **Open request list** (everything still waiting, approved or partly available) and **Roles that grant libraries** (each mapped role with its libraries). Add them with *Customize → More widgets*.
- **Sync now** on the Libraries page (the same full sync as on the dashboard, which also re-reads library names from Plex).

### Changed
- **Alerts that end by themselves clear themselves**: stalls that are over, members' buttons or forms that expired (left unfinished), Seerr/Discord/Plex trouble once the service works again, a reaction post once it publishes. The log lines stay, marked *Resolved by WooSarr* with the reason; real errors, and commands answered too late, still wait for an admin.
- Wording: *Recent activity* says a member "requested media" (not "a title", which reads like a role); the `/request` results menu says "Choose a movie or show".
- The Connections widget's title says when the next full sync runs: "Connections - Next Sync in 12 minutes" (or *Syncing now* / *Sync queued*).
- The dashboard starts with the **Setup checklist** first and full width (it was hidden by default). Admins who already saved their own layout keep it; anyone on the default layout sees the checklist too and can hide it in *Customize*.
- Requests page: open requests stay listed (and keep being checked) however old they are; finished ones (available, declined, failed, removed in Seerr) are listed for 7 days. Open requests are checked every 30 minutes instead of every 5, and no longer stop being checked after 60 days.
- `/help`: `/invite` now says "Library access is determined by possessed roles."
- Settings: admins who sign in with single sign-on have no password, so *My Account* no longer offers a password change and *Admins* no longer offers *Reset password* for them (the API refuses it too; before, a reset quietly turned them into password admins). The Admins list says *Single sign-on* or *Password*.
- Log lines from INFO up (Alerts, Logs page, log files) show who or what an id is: Discord members, roles, channels and the server, and Plex users, e.g. `User: 284362693538742273 (@rWoot715)`. The same problem for different members still counts as one alert.

### Fixed
- The Discord status could crash right after the bot reconnected (`'NoneType' object has no attribute 'guild_permissions'`): Discord had sent the server but not yet the bot's own member. That moment now counts as still connecting.
- The emoji and Time drift tiles said *Discord bot not connected* for up to 30 s after a restart while the bot was still connecting. They now say *Connecting to Discord…*, and fill in as soon as it's connected.
- WooSarr logs at startup how much CPU it may use (cores it may run on, any pinning, any CPU time limit); the *paused* stall warning repeats it.
- The *paused* stall warning also says what the host's CPUs were doing during the pause (busy, waiting on disk, taken by a hypervisor, idle) and how many there are, so a load figure can be read.
- A late Discord command could be blamed on Discord when WooSarr itself had stalled (live: a 14 s gap with no log lines at all). A watchdog now reports every stall of more than 3 s as either *event loop blocked* (with what it was running) or *paused* (with host load, pressure and swap), and late commands are blamed on the stall.
- Discord commands could time out ("The application did not respond"): every slash command now answers Discord before doing any work, the bot keeps a warm connection to Discord, and each command's delivery and answer times are logged (on Discord's clock, so a wrong server clock doesn't skew them). When Discord gives up anyway, the member gets a DM asking them to try again. Suggestions Discord had already discarded while someone typed are no longer logged as errors (the earlier fix didn't catch discord.py's own log line).
- The first web page opened after a restart could stall the Discord connection for over 10 seconds ("heartbeat blocked") while Python loaded a file-serving module from a slow disk. That module is now loaded at startup.
- The dashboard's Connections widget showed a red *Disconnected* while the Discord bot was still connecting; it now shows *Connecting…*.
- Customize → *More widgets*: each entry has a border, and *Blank space* has its own row (the Customize hint says where to find it).
- Integrations (and the Seerr card) showed *Not set up* while still loading; they now show *Checking…* (or *Connecting…* for Discord).
- The dashboard banner counted every occurrence ("5 unresolved errors" for one alert that happened 5 times), so it looked like errors were missing from Alerts. It now counts problems, like the Alerts widget: "1 unresolved error (5 times)".
- Typing a title in the request command searched Seerr on every keystroke, and the suggestions Discord had already discarded showed up as errors in Alerts. WooSarr now waits a moment and searches only for what the member has typed so far, and logs answers Discord discarded quietly (a late answer to a command is a one-line warning).
- `/link` could fail with "The application did not respond" (Discord error 10062) when the bot took over 3 seconds to answer. `/link`, `/unlink` and the Plex username form now answer Discord first and do their checks after; if Discord still drops one, the log says how late it was.
- Members who had reacted with an emoji that was then changed or removed from a reaction post kept that role for good, even after taking their reaction off. Now the role is removed when the post is updated (unless the old reaction is kept working).
- The reaction post emoji picker shows emoji added to the server since WooSarr started (it fetched the list once per session, and the bot didn't receive emoji updates from Discord). It now also has a *Hide animated* switch.

### Security
- Input review: no endpoint turns input into code, SQL, file paths or HTML (see the threat model). Hardening: the Discord bot now defaults to never pinging @everyone, @here or roles, and `%`/`_` in the member search match literally. A hostile-input test sweep runs in CI.

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
