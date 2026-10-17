# WooSarr

Automatically grants and removes **Plex library access** based on **Discord roles**.

- Role grants are **additive** (a user keeps a library if any role, manual grant, or exemption keeps it)
- Per-library **exemptions** that role changes never touch
- Manual per-user grants from the web UI
- Only Plex users with a **linked Discord account** are managed
- Users link themselves with `/link` (Plex username, or Plex sign-in for a verified link; no Plex tokens are stored)
- Web UI on port **31337** with first-run admin setup and optional SAML SSO
- Runs as a single Docker container, with an Unraid template in `unraid/`

> **Status:** early development. See [docs/action-plan.md](docs/action-plan.md) for the full plan, decisions and task status.

## Run

```bash
docker run -d --name woosarr -p 31337:31337 -v /path/to/appdata/woosarr:/config ghcr.io/woosarr/woosarr:latest
```

Open `http://<host>:31337` and follow the setup wizard. Back up the `/config` volume: it holds the database and the key that encrypts your stored secrets.

## Logs and troubleshooting

Logs are available over the API, so you don't need a shell on the host:

```bash
curl "http://<host>:31337/api/v1/logs?level=WARNING&limit=200"   # recent entries (JSON)
curl "http://<host>:31337/api/v1/logs?since=<latest_seq>"        # only newer entries
curl -o woosarr.log "http://<host>:31337/api/v1/logs/file"       # full rotating log file
```

Set `WOOSARR_LOG_LEVEL=DEBUG` for more detail. Secrets are redacted before anything is logged. The logs are also kept in `/config/logs/`.

## Develop

```bash
make install   # backend venv + frontend deps
make check     # lint + tests (backend and frontend) + UI build
make dev       # run on http://localhost:31337 with ./config
```

## Releases

Pushes to `main` and version tags (`v1.2.3`) build a linux/amd64 image and publish it to GitHub Container Registry via `.github/workflows/docker-publish.yml`.

## License

MIT
