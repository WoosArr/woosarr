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

### Members who leave the Discord server

Default: their role-granted libraries are removed; manual grants and exempt libraries stay. Change this under *Settings → General*.

### Verified and unverified links

- **Verified**: the user signed in to Plex during `/link`, or an admin created the link.
- **Unverified**: the user typed a Plex username. It isn't proven. A verified link to the same Plex account replaces it.
- Turn on *Ignore unverified links* to manage only verified users.

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
