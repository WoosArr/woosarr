# Unraid notes (P0-06)

How WooSarr fits Unraid's Docker manager and Community Applications (CA). The template is `unraid/woosarr.xml`.

## Template fields

| Field | Value | Why |
|-------|-------|-----|
| Repository | `ghcr.io/woosarr/woosarr:latest` | Anonymous pull needs the GHCR package to be public (P2-06). |
| Network | `bridge` | One HTTP port; Plex and Discord are outbound only. |
| Icon | `unraid/icon.png` (raw GitHub URL) | 256 px PNG rendered from `assets/brand/woosarr.svg` (`make icons`); CA wants a square PNG. |
| WebUI | `http://[IP]:[PORT:31337]/` | `[PORT:31337]` is replaced with whatever host port the user mapped to container port 31337. |
| Port | container `31337`/tcp, host default `31337` | Users may change the host side; the container side stays 31337. |
| Path | `/config` → `/mnt/user/appdata/woosarr` | Database, logs, `secrets/master.key`. Covered by appdata backup plugins. |
| PUID / PGID | `99` / `100` (nobody:users) | Unraid convention; `docker/entrypoint.sh` remaps the runtime user and chowns `/config`. |
| `WOOSARR_LOG_LEVEL` | `INFO` (advanced) | Raise to `DEBUG` to troubleshoot; read logs in the UI or via `GET /api/v1/logs`. |
| Icon | `https://raw.githubusercontent.com/WoosArr/woosarr/main/unraid/icon.png` | 256×256 PNG with transparent corners; must be on `main` to resolve. |
| TemplateURL | raw URL of `unraid/woosarr.xml` | Lets Unraid offer template updates. |
| Category | `MediaApp:Other Tools:` | CA browse categories. |
| ExtraParams | `--health-start-period=20s` | Matches the Dockerfile healthcheck; Unraid shows health in the Docker tab. |

## Behaviour Unraid relies on

- **Health:** the image `HEALTHCHECK` polls `/api/health` on `WOOSARR_PORT` (default 31337).
- **Stop/update:** Unraid stops containers with `docker stop` (SIGTERM, 10 s then SIGKILL). Uvicorn shuts down within `timeout_graceful_shutdown=5`. CI's `scripts/smoke-test.sh` asserts stop < 10 s.
- **Persistence:** everything stateful is under `/config`; the image is replaceable. Losing `secrets/master.key` means stored secrets must be re-entered (document in P12-03).
- **Plain HTTP:** port 31337 is not TLS. Users who expose it should put it behind a reverse proxy (P10-04).

## Installing before CA listing

1. Copy `unraid/woosarr.xml` to `/boot/config/plugins/dockerMan/templates-user/my-woosarr.xml` on the Unraid box.
2. Docker tab → Add Container → choose the *woosarr* template → Apply.

## CA submission (P12-04)

CA reads templates from a public GitHub repo registered with the CA maintainers (support thread on the Unraid forum). Needs: public image, icon URL that resolves, `Support` link (GitHub issues), and an `Overview`.

## Still to verify on a real box (P2-03, P11-06, HUMAN)

- Install from the template, WebUI link opens the app.
- `/config` ownership with PUID 99 / PGID 100 on an array share.
- Update (pull new image) and restart keep settings and secrets.
