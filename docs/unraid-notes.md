# Unraid notes (P0-06)

How WooSarr fits Unraid's Docker manager and Community Applications (CA). The template is `unraid/woosarr.xml`.

## Template fields

| Field | Value | Why |
|-------|-------|-----|
| Repository | `ghcr.io/woosarr/woosarr:latest` | Anonymous pull needs the GHCR package to be public (P2-06). |
| Network | `bridge` | One HTTP port; Plex and Discord are outbound only. |
| WebUI | `http://[IP]:[PORT:31337]/` | `[PORT:31337]` is replaced with whatever host port the user mapped to container port 31337. |
| Port | container `31337`/tcp, host default `31337` | Users may change the host side; the container side stays 31337. |
| Path | `/config` → `/mnt/user/appdata/woosarr` | Database, logs, `secrets/master.key`. Covered by appdata backup plugins. |
| PUID / PGID | `99` / `100` (nobody:users) | Unraid convention; `docker/entrypoint.sh` remaps the runtime user and chowns `/config`. |
| `WOOSARR_LOG_LEVEL` | `INFO` (advanced) | Raise to `DEBUG` to troubleshoot; read logs in the UI or via `GET /api/v1/logs`. |
| Icon | `https://raw.githubusercontent.com/WoosArr/woosarr/main/unraid/icon.png` | 256×256 PNG with transparent corners, rendered from `assets/brand/woosarr.svg` (`make icons`); must be on `main` to resolve. CA wants a square PNG. |
| TemplateURL | raw URL of `unraid/woosarr.xml` | Lets Unraid offer template updates. |
| Category | `MediaApp:Other Tools:` | CA browse categories. |
| ExtraParams | `--health-start-period=20s` | Matches the Dockerfile healthcheck; Unraid shows health in the Docker tab. |

## Behaviour Unraid relies on

- **Health:** the image `HEALTHCHECK` polls `/api/health` on `WOOSARR_PORT` (default 31337).
- **Stop/update:** Unraid stops containers with `docker stop` (SIGTERM, 10 s then SIGKILL). Uvicorn shuts down within `timeout_graceful_shutdown=5`. CI's `scripts/smoke-test.sh` asserts stop < 10 s.
- **Persistence:** everything stateful is under `/config`; the image is replaceable. Database migrations run on start. Losing `secrets/master.key` means stored secrets must be re-entered (admin guide → *Backup and restore*).
- **Plain HTTP:** port 31337 is not TLS. Users who expose it should put it behind a reverse proxy (README → *HTTPS and reverse proxies*).
- **Logs:** `/config/logs/woosarr.log` (everything) and `/config/logs/alerts.log` (warnings and errors only, kept longer); the Docker tab's log view shows the container's stdout.

## Installing before CA listing

1. Copy `unraid/woosarr.xml` to `/boot/config/plugins/dockerMan/templates-user/my-woosarr.xml` on the Unraid box.
2. Docker tab → Add Container → choose the *woosarr* template → Apply.

## Community Applications (P12-06)

Submissions go through https://ca.unraid.net/submit (docs and a video walkthrough there). Requirements and where WooSarr stands:

| Requirement | WooSarr |
|-------------|---------|
| Public, active GitHub repo with an OSI-approved licence | `WoosArr/woosarr`, MIT |
| `ca_profile.xml` in the repo root with a non-empty `<Profile>` | `ca_profile.xml` (overview, icon, web page, a photo) |
| One XML per Docker app, `<Repository>` pointing at a valid image, readable `<Name>` and `<Overview>` | `unraid/woosarr.xml` → `ghcr.io/woosarr/woosarr:latest` (public) |
| `<Support>` or `<Project>` | both (GitHub issues, repo) |
| `<TemplateURL>` = raw URL of that exact file | yes |
| Optional: icon, screenshots, `Requires`, `ReadMe`, `License`, search terms | all set; screenshots are `docs/images/*.png` (demo data, `make screenshots`) |

To submit: open https://ca.unraid.net/submit/new, enter the repository, run **Validate** and **Scan** (they parse the XML, read `ca_profile.xml` and check for duplicates), fix anything flagged (the category string is the likeliest; valid strings come from the *Application Categorizer* plugin in Apps), then submit. Approved apps appear after the next catalog build. An Unraid forum support thread is optional; if you open one, point `<Support>` in the template and `<Forum>` in `ca_profile.xml` at it.

## Verified on a real box

- Install from the template; the WebUI link opens the app (2026-10-19).
- Update (pull new image) and restart keep settings and secrets.
