# WooSarr admin guide

How WooSarr decides who sees which Plex library, and how to look after it.

## How access is decided (P12-02)

WooSarr manages a Plex user only if they are **linked** to a Discord account. For each linked user it computes the complete list of libraries they should have, and writes that list to Plex.

| Source | Meaning | Shown as |
|--------|---------|----------|
| **Role** | A Discord role the member holds is mapped to the library (Roles page). | `role` |
| **Manual** | An admin granted it on the Users page. Kept no matter which roles the user has. | `manual` |
| **Exempt** | The library is exempt (Libraries page). Role changes never add or remove it; whatever the user has stays. | `exempt` |
| **External** | Shared in Plex directly and no role manages that library. WooSarr leaves it alone. | `external` |

The rules:

1. **Additive.** A user gets the union of all their roles' libraries. Losing one role only removes a library if no other role, manual grant or exemption keeps it.
2. **Exempt libraries are frozen.** Role logic never touches them, in either direction.
3. **Unmanaged libraries are left alone.** A library no role maps to isn't removed from anyone.
4. **Only linked users.** Unlinked Plex users are never changed. Unlinking someone stops management; their current shares stay.
5. **Complete sets.** Plex replaces the whole share list on every update, so WooSarr always sends the full list (never a partial one) and re-reads the user's live shares right before writing. If they changed in the meantime, it skips that user and tries again on the next sync.

### When syncs happen

- Right away (after a few seconds of debounce) when a member's roles change, a role is edited or deleted, a member joins or leaves, a link changes, or you change a manual grant.
- A full reconcile when you change a role mapping, an exemption, or certain settings, and on a schedule (default every 15 minutes).
- **Sync now** on the dashboard.

### Discord commands

- `/help`: lists the commands below with their options (only the asker sees it; `/request` appears only when Seerr is set up).
- `/link`: link your Plex account (Plex sign-in = verified, or typed username). *Settings → Linking → Who may link by typing a Plex username* can limit the typed-username option to chosen roles; everyone else only sees Plex sign-in, and a typed username from them is refused (audited as `link_failed`, reason `username_link_not_allowed`).
- `/unlink`: remove your own link. What happens to access is set under *Settings → Linking → When a member unlinks themselves*.
- `/request title` (name set in *Settings → Requests*, default `/request`): request a movie or show through Seerr as the member's own Seerr user. Needs Seerr connected on the Integrations page and a linked (by default verified) Plex account. See [requests.md](requests.md).
- `/invite email [member]`: send a Plex invite whose libraries come from the member's roles. Members can invite themselves if they hold a role chosen under *Settings → Linking → Plex Invites*; Discord admins can invite for anyone. Accepted invites link automatically; pending ones are listed on the Users page. Not available in dry-run.

### Discord roles managed by WooSarr

- Link-status roles (Verified / Unverified / Linked), per-library roles and reaction posts (react to get a role): see `docs/discord-notes.md`.
- Bot permissions per feature, and how to grant only what you need: `docs/discord-notes.md` → *Bot permissions by feature*.

### Removing access by hand

Users page → select users → **Remove libraries**. Each library shows what the selected users have now (from the last sync): ✓ all have it, – some do, empty: nobody (disabled). Untick a library to remove it from everyone who has it; the dialog previews who loses what before you confirm, **Reset** clears your changes, and after removing, **Undo** gives everything back (including deleted manual grants; audited as `access_restored_admin`). It removes those libraries from the users in Plex now, together with any manual grants for them (dry-run respected, audited as `access_removed_admin`). For a linked user, libraries their Discord roles grant are kept and listed, because the next sync would add them back: remove the role in Discord or change the role mapping instead. For unlinked users WooSarr doesn't manage access, so nothing re-adds a removed library.

### Members who leave the Discord server

Default: their role-granted libraries are removed; manual grants and exempt libraries stay. Change this under *Settings → Linking*.

### Verified and unverified links

- **Verified**: the user signed in to Plex during `/link`, or an admin created the link.
- **Unverified**: the user typed a Plex username. It isn't proven. A verified link to the same Plex account replaces it.
- Turn on *Ignore unverified links* to manage only verified users.

## Dashboard

The dashboard is made of widgets on an 8-column grid. **Customize** lets each admin move (↑/↓), resize (**eighth**, **quarter**, **half** or **full** width; 4 columns on tablets, 2 on phones) and hide widgets, choose how chartable widgets are drawn (their usual form, a **Circle** or **Bars**), and add hidden ones back from *More widgets*; **Save layout** stores it for your admin account, **Reset to default** brings back WooSarr's layout.

Circle and bar views exist for the number tiles (e.g. Plex users by link status, linked share, libraries by how they're managed, roles that give libraries, requests by status, invites), Alerts (errors / warnings / resolved), Link coverage, Access changes (applied vs planned), Recent activity (by kind), Library access (users per library), Requests and Security. Every chart has a legend with the numbers, and the colours follow the colour-vision palette in Settings → Appearance. Widgets:

| Widget | Shows |
|--------|-------|
| Connections | Plex, Discord and Seerr status |
| Plex users / Linked users / Libraries / Discord roles / Open requests / Pending invites | One number each, linking to its page |
| Alerts | Open warnings and errors grouped by problem (repeats with different ids count once), plus **Account** alerts, with **Resolve**. No time limit: an alert stays until resolved |
| Last sync / Pending changes | The last sync report and what the next one would change |
| Link coverage | Verified / unverified / unlinked Plex users, and Discord members who haven't linked |
| Access changes (14 days) | Applied changes per day (and planned ones in dry-run) |
| Recent activity | The latest audited events, without routine sync and sign-in entries |
| Library access | Users per library; marks exempt libraries and libraries no role manages |
| Requests / Plex invites | Recent requests by status; pending invites |
| Setup checklist | Connections, role mappings, unmanaged libraries, dry-run |
| Security | Failed sign-ins (24 h), active sessions, API keys |

**Resolve** hides that problem from the dashboard (the banner count and the widget), including earlier occurrences, and keeps hiding new ones for 24 hours; it's audited (`error_resolved`) and shared by all admins. If it happens again after the 24 hours, it shows up again. *Show resolved* lists resolved problems with **Unresolve**. The Logs page always shows everything. Warnings and errors are also written to their own log file, `/config/logs/alerts.log` (warnings and errors only, 2 MB × 3 files, so it holds far more history than the busy main log). The latest 2,000 are reloaded from it when the container starts, so a restart doesn't clear the dashboard. Download it from the API with `GET /api/v1/logs/file?name=alerts`.

**Account alerts** come from the audit log, so they survive restarts for as long as audit entries are kept. They name everyone involved:
- someone tried to link a Plex account that's linked to another member (by typed username or Plex sign-in), refused; repeats by the same member on the same account count as one alert;
- someone took over an unverified link by signing in with Plex (the other member's link was removed);
- an admin moved a Plex account's link from one member to another on the Users page.

## Impact previews

Role mapping changes (Roles page), Remove libraries (Users page) and settings that change access (dry-run, leave-server policy, ignore unverified links) show who gains or loses which libraries before you save. Previews use the shares seen at the last sync and make no Plex calls.

## Dry-run

New installs start in **dry-run**: WooSarr computes, logs and audits every change (`Access change (dry run)` in the audit trail) without touching Plex. Check the dashboard's *Pending changes*, then turn dry-run off in *Settings → General*.

## Troubleshooting

- **Logs page**: live application log; filter by level or search for a request id from an error message.
- **Audit trail** (Logs → Audit trail): every access change with who/what/why, every role mapping, link and settings change.
- Set *Log level* to DEBUG in Settings to see every Plex and Discord call (secrets are always redacted).
- **Remote troubleshooting without a shell**: create a **read** API key (*Settings → API keys*) and use it:

  ```bash
  curl -H "Authorization: Bearer wsk_…" "http://<host>:31337/api/v1/logs?level=WARNING&limit=200"
  curl -H "Authorization: Bearer wsk_…" "http://<host>:31337/api/v1/status"
  ```

  Revoke the key when you're done.

## Admin accounts

- Single sign-on (Okta, Entra ID, Google, ADFS, Custom OIDC): *Settings → Single sign-on*; setup steps per provider in `docs/sso.md`.

- *Settings → Admins*: add, disable, delete, reset passwords, unlock locked accounts. The last active admin can't be disabled or deleted.
- Locked out or forgot the password? Open the container console (Unraid: Docker tab → WooSarr icon → *Console*) and run:

  ```bash
  python -m woosarr.admin list
  python -m woosarr.admin reset-password admin --generate   # prints a new password
  python -m woosarr.admin unlock admin
  ```

## Backup and restore (P12-03)

Everything WooSarr needs is in `/config` (`/mnt/user/appdata/woosarr` on Unraid):

| Path | What | Back up? |
|------|------|----------|
| `app.db` (+ `app.db-wal`, `app.db-shm`) | settings, mappings, links, audit, encrypted secrets | Yes |
| `secrets/master.key` | key that decrypts the stored Plex/Discord tokens | Yes, **separately** from `app.db` |
| `logs/` | rotating application logs | Optional |

- Stop the container (or use the Unraid *Appdata Backup* plugin, which stops it) so the SQLite files are consistent.
- **Restore**: put the files back in `/config` and start the container. The schema migrates forward automatically.
- **Lost `master.key`**: settings, mappings and links are fine, but the stored Plex and Discord tokens can't be decrypted. Delete them by re-running the Plex and Discord setup, which stores new tokens.
- **Keep the key elsewhere**: set `WOOSARR_MASTER_KEY_FILE=/run/secrets/woosarr_key` (Docker secret) or `WOOSARR_MASTER_KEY` so a DB backup alone can't reveal tokens.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `WOOSARR_PORT` | `31337` | Port inside the container |
| `WOOSARR_LOG_LEVEL` | `INFO` | Startup log level (the Settings page can override it) |
| `WOOSARR_LOG_FORMAT` | `json` | `json` or `text` for the container log |
| `WOOSARR_MASTER_KEY` / `WOOSARR_MASTER_KEY_FILE` | — | Supply the encryption key instead of `/config/secrets/master.key` |
| `WOOSARR_COOKIE_SECURE` | `auto` | `always` behind an HTTPS proxy that isn't in `WOOSARR_FORWARDED_ALLOW_IPS` |
| `WOOSARR_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxy IPs whose `X-Forwarded-*` headers are trusted |
| `WOOSARR_SESSION_IDLE_HOURS` / `WOOSARR_SESSION_MAX_DAYS` | `12` / `7` | Session lifetime |
| `WOOSARR_SETUP_CODE_REQUIRED` | `true` | First admin needs the code from the container log |
| `PUID` / `PGID` | `99` / `100` | File ownership in `/config` |
| `WOOSARR_DEMO` | `false` | Fake Plex/Discord data for trying the UI. Never on a real install. |
