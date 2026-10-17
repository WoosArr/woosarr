# WooSarr

Automatically grants and removes **Plex library access** based on **Discord roles**.

- Role grants are **additive**: a user keeps a library if any role, manual grant or exemption keeps it
- Per-library **exemptions** that role changes never touch
- Manual per-user grants from the web UI
- Only Plex users with a **linked Discord account** are managed
- Users link themselves with `/link` (Plex username, or Plex sign-in for a verified link; no Plex tokens are stored)
- Starts in **dry-run**: see exactly what would change before anything does
- Web UI on port **31337**: light/dark themes, colour-blind palettes, live logs and an audit trail
- One Docker container, with an Unraid template in `unraid/`

> **Status:** the web UI, auth, sync engine and audit are done. Plex and Discord connections are being finished against real servers. See [docs/action-plan.md](docs/action-plan.md).

## Install

### Unraid

1. Copy [`unraid/woosarr.xml`](unraid/woosarr.xml) to `/boot/config/plugins/dockerMan/templates-user/my-woosarr.xml`.
2. Docker tab → **Add Container** → choose **woosarr** → **Apply**.
3. Open the WebUI. Details: [docs/unraid-notes.md](docs/unraid-notes.md).

### Docker

```bash
docker run -d --name woosarr \
  -p 31337:31337 \
  -v /path/to/appdata/woosarr:/config \
  -e PUID=99 -e PGID=100 \
  ghcr.io/woosarr/woosarr:latest
```

Images are published for `linux/amd64` and `linux/arm64`.

## First run

1. Find the **setup code** in the container log (Unraid: Docker tab → WooSarr icon → **Logs**):

   ```
   ================================================================
    WooSarr first-run setup code: 1a2b-3c4d-5e6f
   ```

2. Open `http://<host>:31337`, enter the code and create the first admin account (password ≥ 12 characters).
3. **Setup** page: connect Plex and Discord.
4. **Roles** page: choose which libraries each Discord role grants. **Libraries** page: exempt any library that roles should never touch.
5. **Dashboard**: check *Pending changes*. When they look right, turn off **dry-run** in *Settings → General*.

How access is decided, troubleshooting, backups and all settings: [docs/admin-guide.md](docs/admin-guide.md).

## HTTPS and reverse proxies

Port 31337 is plain HTTP. On anything other than a trusted home network, put WooSarr behind a reverse proxy with TLS (Nginx Proxy Manager, SWAG, Caddy, Traefik…), and tell WooSarr to trust the proxy's forwarded headers so session cookies are marked `Secure`:

```bash
-e WOOSARR_FORWARDED_ALLOW_IPS=172.18.0.2   # your proxy's IP as seen by the container
# or, if that's awkward:
-e WOOSARR_COOKIE_SECURE=always
```

Caddy example:

```
woosarr.example.com {
    reverse_proxy woosarr:31337
}
```

Don't expose port 31337 directly to the internet.

## Logs and troubleshooting

- **Logs** page: live application log and the audit trail of every access change.
- Without a browser session, create a **read** API key (*Settings → API keys*) and:

  ```bash
  curl -H "Authorization: Bearer wsk_…" "http://<host>:31337/api/v1/logs?level=WARNING&limit=200"
  curl -H "Authorization: Bearer wsk_…" -o woosarr.log "http://<host>:31337/api/v1/logs/file"
  ```

- Set the log level to DEBUG in *Settings → General* (or `WOOSARR_LOG_LEVEL=DEBUG`). Secrets are always redacted.
- Forgot the admin password: `python -m woosarr.admin reset-password <user> --generate` in the container console.

## Backups

Back up `/config` (the database, logs and `secrets/master.key`). The master key decrypts the stored Plex and Discord tokens; keep a copy somewhere other than the database backup, or supply it with `WOOSARR_MASTER_KEY_FILE`. See [docs/admin-guide.md#backup-and-restore-p12-03](docs/admin-guide.md#backup-and-restore-p12-03).

## FAQ

**Will WooSarr remove libraries I shared by hand?** Only libraries that a role manages. Libraries no role maps to, and exempt libraries, are left exactly as they are.

**A user lost a role but still has the library.** Another role, a manual grant or an exemption still gives it; the Users page shows each library's source.

**Someone linked the wrong Plex account.** Unlink them on the Users page. A Plex sign-in (verified) link replaces a typed-username (unverified) claim. You can also ignore unverified links entirely in Settings.

**Does WooSarr store Plex passwords or user tokens?** No. `/link` sign-ins (arriving with the Discord bot) use a one-time token that is invalidated straight after reading the username. Only the server owner's Plex token and the bot token are stored, encrypted.

**Can I try it without connecting anything?** Yes: `WOOSARR_DEMO=true` uses a fake Plex server and Discord data. Don't use it on a real install.

## Develop

```bash
make install   # backend venv + frontend deps
make check     # lint + tests (backend and frontend) + UI build + contrast check
make demo      # run on http://localhost:31337 with fake Plex/Discord data
make e2e       # Playwright UI tests
```

Security notes: [docs/threat-model.md](docs/threat-model.md).

## Releases

Pushes to `main` and version tags (`v1.2.3`) run CI (lint, tests, UI tests, audits, secret scan), smoke-test and secret-scan the image, then publish `linux/amd64` and `linux/arm64` images to GitHub Container Registry.

## License

MIT
