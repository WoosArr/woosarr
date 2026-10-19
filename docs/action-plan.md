# WooSarr — Action Plan (written for Claude Code)

> **Read this first.** You are an agent working in this repo. This file is the source of truth for scope, decisions, status and next steps. `CLAUDE.md` points here.
>
> **Operating loop**
> 1. Pick the first task in §9 that is `[ ]` or `[~]`, whose *Depends* are all `[x]`, and that is not marked **HUMAN** (see §8).
> 2. Do the work. Run the task's **Verify** command(s). Only when they pass, change the checkbox and the *Status snapshot* (§1) in the same commit.
> 3. Commit per task: `P5-01: short description`, then **`git push`** (user rule). Pushes to `main` run CI and publish a Docker image only if CI passes (§3, D14), so never commit half-done work.
> 4. If a decision is `OPEN` or a task is **HUMAN**-gated, stop and ask. Record answers in §3.
> 5. Keep task IDs stable. Add new tasks with the next free ID in the phase.
>
> Legend: `[x]` done and verified · `[~]` partly done (note says what remains) · `[ ]` todo · `[-]` deferred · **HUMAN** needs a real Plex server / Discord bot / IdP / Unraid box / GitHub settings.

---

## 1. Status snapshot (2026-10-17)

- **Repo:** https://github.com/WoosArr/woosarr, branch `main`. Local clone: `~/Documents/GitHub/git-WoosArr/woosarr`.
- **Port:** **31337** (D15; was 2000).
- **Done and tested (192 backend tests + 6 opt-in skipped, 2 vitest, 11 Playwright incl. axe):** sync engine, apply layer with dry-run, secrets manager, log redaction, link rules + one-shot token capture, config loader, logging (stderr JSON + rotating file + in-memory buffer, all redacted), FastAPI app factory (`/api/v1`, error handler with request ids, SPA serving), **logs API**, SQLite + migrations, admin auth (sessions, CSRF, lockout, API keys, first-run setup code), sync service (worker, debounce, periodic reconcile, audit), full domain API, the **web UI** (all pages, themes, colour-vision palettes), recovery CLI, multi-arch image with smoke test and secret scan.
- **Ran for real locally:** `python -m woosarr` serves health, UI, logs on :31337 and stops cleanly on SIGTERM (~0.2 s).
- **CI/CD:** `docker-publish.yml` calls `ci.yml` (ruff, pytest, vitest, frontend build, gitleaks), then builds `linux/amd64`, **smoke-tests the image** (`scripts/smoke-test.sh`: health, Docker healthy, `/config` persistence across restart, `docker stop` < 10 s) and only then pushes to GHCR. Gated runs green through `d68c5e8`, including the image smoke test; that image is published as `latest`. No Docker on the dev Mac, so the image is only exercised in CI.
- **Diagnostics:** nobody has shell access to the deployed server (D16, http://10.0.1.101:31337). Logs come from `GET /api/v1/logs` (since/level/q/limit) and `GET /api/v1/logs/file`, which need an admin session or a `read` API key (`Authorization: Bearer wsk_…`). Claude reads the key from `~/.config/woosarr/api-key` and never prints it.
- **Live (http://10.0.1.101:31337):** admin set up; Plex signed in (*Bragi*, 15 libraries, 63 users); Discord bot connected (*Dave's Plex Chat*); write check passed; still in dry-run with no role mappings or links yet. Remaining live checks: a real `/link` (P0-07, P4-04) and going live with mappings (P11-04).
- **Live server:** running the new image; waiting for first-run setup (setup code is in the Unraid container log).
- **First thing to do in a fresh session:** `make install && make check`, and `gh run list --workflow "Docker publish" --limit 3`; fix anything red before new work.

## 2. Commands

```bash
# Everything (repo root): venv in backend/.venv, lint + tests for both halves, UI build
make install && make check
make dev                     # backend on :31337 with ./config (git-ignored) and the built UI
# Backend by hand (CI uses Python 3.12)
cd backend && pip install -e ".[dev]" && ruff check . && python -m pytest -q
# Frontend by hand
cd frontend && npm ci && npm test && npm run build
# Image (needs Docker) and smoke test
make image && make smoke     # = scripts/smoke-test.sh woosarr:dev 31337
curl -fsS localhost:31337/api/health
curl -fsS "localhost:31337/api/v1/logs?level=WARNING&limit=100"
# Pre-commit hooks (ruff + gitleaks)
pip install pre-commit && pre-commit install
# CI
gh run list --workflow "Docker publish" --limit 3
gh run view <id> --log-failed | grep -v '^$' | tail -60
```

## 3. Decisions

| ID | Decision | Status | Answer |
|----|----------|--------|--------|
| D1 | Backend | DECIDED | Python 3.12, FastAPI, `discord.py`, **python-plexapi** (install name on PyPI: **`PlexAPI`**, import `plexapi`). plexapi.dev TypeScript SDK rejected (generated, beta, no library-sharing methods found). |
| D1a | Frontend types | DECIDED | Generate TS types from FastAPI's OpenAPI schema (`openapi-typescript`). |
| D1b | Plex boundary | DECIDED | All Plex access via our `PlexGateway` interface (`backend/woosarr/plex_gateway.py`). |
| D2 | Frontend | DECIDED | React + TypeScript + Vite; built to static files served by FastAPI (one container, one port). |
| D3 | Database | DECIDED | SQLite in `/config` (SQLAlchemy + Alembic). |
| D4 | Linking | DECIDED | Discord `/link` → ephemeral reply with two options: **(1)** enter Plex username (unverified) or **(2)** authenticate to Plex via OAuth to capture the username (verified). **No credentials/tokens are stored**; the OAuth token is used once and invalidated. Users are trusted "to a point". Link records `method` (`username`/`oauth`/`admin`) and `verified`. Admins can link and **unlink** any account. |
| D4a | Link abuse limits | DECIDED | Per-user cooldown; one Plex account ↔ one Discord account; a verified link may supersede an unverified claim (`linking.decide_link`); all link events audited. |
| D5 | Secrets at rest | DECIDED | Fernet-encrypted in DB; master key from `WOOSARR_MASTER_KEY` / `WOOSARR_MASTER_KEY_FILE` / auto-generated `/config/secrets/master.key` (0600). Implemented in `core/secrets.py`. |
| D6 | Discord token | DECIDED | Entered once in the setup wizard, stored encrypted. |
| D7 | UI auth | DECIDED | First-run wizard creates the initial local admin (argon2id). **SAML 2.0** SSO for an external IdP; local login stays as break-glass unless disabled. |
| D7a | SAML | DEFERRED | Not needed (user, 2026-10-17); replaced by OIDC (D21). |
| D21 | Admin SSO | DECIDED | **OpenID Connect**, multiple providers, presets for Okta, Entra ID, Google, ADFS + Custom; code flow + PKCE; access only via per-provider rules; admins linked by (provider, sub); local login is break-glass with a lockout guard (user, 2026-10-17). |
| D22 | Media requests | DECIDED | Movies/TV through **Seerr** (Overseerr/Jellyseerr compatible) as the member's own Seerr user (`X-API-User`), so Seerr's permissions, quotas and approval apply. Slash command name configurable, default **`/request`** (user, 2026-10-17); verified links only by default; status by polling (no webhook). Ombi, direct *arr, Listenarr later. |
| D8 | Triggers | DECIDED | Discord gateway events + periodic full reconcile (default 15 min, configurable). |
| D9 | Library id | DECIDED | Plex section key + server machine identifier. |
| D10 | Name | DECIDED | **WooSarr** (image `ghcr.io/woosarr/woosarr`, package `woosarr`, appdata `/mnt/user/appdata/woosarr`). |
| D11 | Scope | DECIDED | One Discord guild, one Plex server in v1. |
| D12 | Dry-run | DECIDED | Global toggle; logs intended changes only. |
| D13 | Source control | DECIDED | Git, branch `main`, GitHub. |
| D14 | CI/CD | DECIDED | GitHub Actions. `docker-publish.yml` = CI (reusable `ci.yml`) → build → image smoke test → push to GHCR on `main` and `v*.*.*` tags, `GITHUB_TOKEN` only. PRs run CI only. `linux/amd64` only for now. Every commit is pushed (user rule). |
| D15 | Port | DECIDED | **31337** (container and default host port). Overrides the 2000 in `docs/specsheet.md`, which is kept read-only as the original spec. Set by `WOOSARR_PORT`. |
| D19 | Plex sign-in | DECIDED | Plex OAuth (PIN flow) **through the web UI** only; no tokens via files or env (user, 2026-10-17). |
| D20 | Discord token | DECIDED | Entered **through the web UI**, validated with Discord (incl. Members intent flag) before it's stored encrypted (user, 2026-10-17). |
| D17 | App log storage | DECIDED | Rotating file + in-memory buffer only; no `app_logs` table (avoids SQLite write load from logging). Audit events go to `audit_log` *and* the app log. |
| D18 | Safe defaults | DECIDED | Fresh installs start in **dry-run**; first admin needs the setup code from the container log. |
| D16 | Diagnostics | DECIDED | No access to the deployed server. All diagnostics come from logs read through the API (`/api/v1/logs`, `/api/v1/logs/file`). New features must log enough (redacted) to be debugged that way; raise `WOOSARR_LOG_LEVEL=DEBUG` when needed. |

## 4. Non-negotiables

- **N1** No hardcoded credentials/tokens anywhere (code, image, tests, logs, git history).
- **N2** Plex auth via OAuth.
- **N3** Role grants are **additive**; removing one role never removes a library another role, a manual grant, or an exemption keeps.
- **N4** Exempt libraries are never added/removed by role logic; current state is preserved.
- **N5** Only modify Plex users with a **linked** Discord account.
- **N6** Plex share updates may replace the full library list: read current state, compute the **complete** target, write it. Never send a partial set. Revoking the last library likely needs `removeSections=True`, not an empty list (confirm in P0-03).
- **N7** Secrets never appear in logs, API responses or UI (masked / "set" indicator only).
- **N8** UI: sidebar nav, light + dark modes, colorblind option(s), purple as a featured colour; never rely on colour alone.
- **N9** `/link` must never persist Plex credentials/tokens; any token is used once, then invalidated.
- **N10** Every web route requires auth except health, login, SAML metadata/ACS, and the first-run wizard (only while no admin exists). Enforced by `api/deps.py` (`require_admin`, `PUBLIC_PATHS`) and `tests/test_app.py::test_every_non_public_route_requires_admin`. The SPA shell (static files) is public; it holds no data.

## 5. Architecture

```
Container (port 31337)
├── FastAPI: /api/v1/* (REST), /api/health, / (built React bundle), auth/session middleware
├── PlexGateway (python-plexapi impl + FakePlexGateway)
├── Discord service (discord.py) in the same event loop
├── Sync engine (pure) + apply layer ← backend/woosarr/sync/
├── Scheduler (periodic reconcile), Secrets manager, redacting logger
└── SQLite /config/app.db (+ /config/secrets/master.key)
```

Current layout (planned items in *italics*):
```
backend/pyproject.toml
backend/woosarr/{__init__,__main__,main,linking,plex_gateway}.py   (python -m woosarr runs the server)
backend/woosarr/api/{deps,logs}.py               + routers per feature (planned)
backend/woosarr/core/{config,logs,secrets,redaction}.py   + db, auth (planned)
backend/woosarr/sync/{engine,apply}.py           + scheduler (planned)
backend/tests/                                   (unittest-style, run by pytest)
frontend/{package.json,package-lock.json,vite.config.ts,src/{main.tsx,api.ts,api.test.ts}}
docker/{Dockerfile,entrypoint.sh}   unraid/{woosarr.xml,icon.png}   scripts/smoke-test.sh
.github/workflows/{ci,docker-publish}.yml   .gitleaks.toml   .pre-commit-config.yaml   Makefile
docs/{action-plan,specsheet,unraid-notes}.md   CLAUDE.md   README.md   LICENSE
```

Data model (adjust in P1-03): `settings`, `secrets`, `plex_users`, `discord_links` (plex_user_id, discord_user_id — both unique, `method`, `verified`, `linked_at`, `linked_by`; **no tokens**), `admin_users` (argon2id hash, `auth_source` local|saml), `saml_config`, `sessions`, `discord_roles_cache`, `discord_members_cache`, `libraries` (`is_exempt`), `role_library_map`, `manual_grants`, `library_state` (source role|manual|external), `audit_log`, `app_logs`.

## 6. Sync rules (implemented in `sync/engine.py` — read the code and tests, they are the spec)

```
role_libs        = ∪ role_library_map[r] for r in user's roles       (unknown/deleted roles grant nothing)
managed_universe = ∪ all role_library_map values − exempt
desired_managed  = (role_libs ∪ manual) ∩ managed_universe
preserved        = current ∩ (exempt ∪ not_in_managed_universe)
target           = desired_managed ∪ preserved ∪ manual              # always the COMPLETE set
remove_all       = target empty and current non-empty                # use the remove-all path
```
`apply.py` re-reads live state right before writing and skips if it drifted (risk R2). Dry-run never writes. Policy for members who left the server: drop role-derived access, keep manual + exempt (setting, default).

## 7. Pitfalls already hit (don't repeat)

- **PyPI name:** it is `PlexAPI`, not `python-plexapi` (Docker build failed on this).
- **Push protection:** a token-shaped string in source blocks the push to GitHub. Assemble fake tokens at runtime (see `tests/test_redaction.py`). `.gitleaks.toml` allowlists that test file.
- **Ruff 0.17+ default rules are strict** (e.g. `BLE001` on bare `except Exception`). Run `ruff check .` before committing; use `# noqa: <rule>` with a reason only when intentional.
- **Pushing to `main` publishes an image** and CI now gates it. Don't push half-done work.
- **`plex.tv/api/v2/friends` is 410 Gone**; use `clients.plex.tv/api/v2/friends` or community GraphQL `allFriendsV2` (uuid ids, share by `invited_email`). Discord DEBUG dumps contain interaction tokens: redact and cap discord.* at INFO.
- **`plex.tv/api/users` only lists users with a share.** Friends without libraries and users whose last library was removed are missing; merge `/api/v2/friends` and `/api/home/users` (see plex-notes). The owner isn't in any friends list; it's added from the account and never synced.
- **DB commits must happen before the response** (`Depends(get_session, scope="function")` in `api/deps.py`). The default scope commits after the response is sent; Playwright caught login → immediate status check racing the commit.
- Playwright: wait for list filtering/async content (`toHaveCount`) before clicking; the demo inventory loads ~1 s after boot.
- **gitleaks action needs a licence on org repos**; CI runs the `zricethezav/gitleaks` container instead.
- `frontend/package-lock.json` is committed; CI and the Dockerfile use `npm ci`. Add packages with `npm install -D pkg@x.y.z`.
- **Don't serve the live log file with `FileResponse`**: it grows while streaming and breaks Content-Length ("Response content longer than Content-Length"). `/api/v1/logs/file` returns a snapshot.
- Ruff run from the repo root (pre-commit) doesn't know `woosarr` is first-party; `known-first-party` is set in `pyproject.toml`.
- `docs/specsheet.md` is read-only on purpose; record changes to it as decisions in §3.
- Library loggers (`uvicorn*`, `discord`, `plexapi`) are routed to the redacting root handlers by `core/logs.py`; run uvicorn with `log_config=None` (as `__main__.py` does) or they bypass redaction.
- python-plexapi is **synchronous**: run Plex calls in a thread pool, never on the event loop.

## 8. Human gates (stop and ask)

| Gate | Needs | Blocks |
|------|-------|--------|
| Real Plex server + test account(s) | Owner signs in via Setup → Plex; runs the write check on a test friend | P0-02, P0-03, P0-07, P3-03..05 (live), P11-04 |
| Discord application/bot + test guild | Bot token entered via Setup → Discord | P4-04 (live), P9-01, P11-04 |
| ~~SAML IdP~~ | deferred (D7a) | — |
| GitHub settings | Package visibility (public), Actions permissions, branch protection | P2-06 |
| Unraid box | Template install test | P2-03, P11-06 |

## 9. Tasks

### Phase 0 — Discovery
- [x] **P0-00** Git repo, `.gitignore`, LICENSE, README, pushed to GitHub. *Verify:* `git ls-remote origin main`.
- [x] **P0-01** Decisions D4, D7, D10 resolved (§3).
- [x] **P0-02** Plex auth: PIN/OAuth flow, stable per-install client id, stored session = encrypted owner token; JWT login not needed (`docs/plex-notes.md`). **Live:** owner signed in through Setup → Plex (2026-10-17, audit `plex_signed_in`).
- [x] **P0-03** Share semantics from the python-plexapi source + **live write check passed** on server *Bragi* (2026-10-17, user 6101685): rewrite same set, add one library (old five kept → replace semantics), restore; friend settings unchanged at every step. (b)/(f) confirmed by the read path; (c) remove-last covered by tests and exercised live when a test user starts with no libraries; (e) this server has no Home users. Results: audit `plex_write_check`, `docs/plex-notes.md`.
- [x] **P0-04** SDK chosen: python-plexapi (PyPI `PlexAPI`).
- [x] **P0-05** Discord facts: `docs/discord-notes.md` (intents + flag check, invite scopes/permissions, guild command registration, ephemeral replies, buttons + modal).
- [x] **P0-06** Unraid template requirements (appdata, port, icon, WebUI). *Verify:* `docs/unraid-notes.md`.
- [x] **P0-07** `/link` token invalidation: throwaway client id, `signout()`, then a fresh request must 401, else the link fails. **Live** 2026-10-17 17:17: `link token invalidated and verified dead`, verified link created.
- [-] **P0-08** SAML — deferred (D7a).

### Phase 1 — Foundation
- [x] **P1-01** Tooling: `.gitignore`, `.gitattributes`, `.dockerignore`, `pyproject.toml`, CI, `.pre-commit-config.yaml` (ruff + gitleaks), vitest, `Makefile`. *Verify:* `make check` runs lint + tests for both halves.
- [x] **P1-02** Config loader `core/config.py` (`WOOSARR_*` env, everything persistent under `config_dir`, no secret settings). *Verify:* `pytest tests/test_config.py`; app boots with only a `/config` volume (smoke test).
- [x] **P1-03** SQLAlchemy models (`db/models.py`) + Alembic (`db/migrations`, run on every start) for the §5 data model; UTC-aware datetimes, SQLite WAL + foreign keys. App logs are *not* stored in the DB (D17). *Verify:* `pytest tests/test_db.py` (fresh DB migrates, models == migrations, one-to-one links, cascades, no token columns).
- [x] **P1-04** Secrets manager — `core/secrets.py`. *Verify:* `pytest tests/test_secrets.py`.
- [x] **P1-05** Logging. Done: redaction; `core/logs.py` (JSON to stderr, rotating `/config/logs/woosarr.log`, in-memory ring buffer; `RedactingFilter` on every handler; library loggers routed through them); logs API with cursor polling (`?since=`) and file download. DB persistence dropped (D17); live tail polls with `since` (P8-01). *Verify:* `pytest tests/test_logs.py` (canary never reaches any sink).
- [x] **P1-06** FastAPI app: `create_app()` factory, `python -m woosarr`, `/api/v1` router + `/api/health` alias, JSON 500 handler that logs the traceback with a request id and leaks no details, `X-Request-ID`, SPA serving with traversal guard. Ran for real on :31337 and in the container (CI smoke test, `d68c5e8`). Sessions moved to P6-01. *Verify:* `pytest tests/test_app.py`; `curl localhost:31337/api/health` → 200 in the container (`scripts/smoke-test.sh`).
- [x] **P1-07** OpenAPI → TypeScript: committed snapshot `frontend/openapi.json` (`make openapi` = `python -m woosarr.openapi` + `npm run gen`), types generated into git-ignored `src/generated/` before every build/test. *Verify:* `tests/test_openapi.py` fails on a stale snapshot; regenerated types break `tsc` where the UI no longer matches.

### Phase 2 — Container and CI/CD
- [x] **P2-01** Dockerfile + entrypoint (PUID/PGID, gosu, healthcheck on `WOOSARR_PORT`, `CMD python -m woosarr`). `scripts/smoke-test.sh` green in CI (`d68c5e8`): health, healthy status, `/config` persistence across restart, graceful SIGTERM. *Verify:* smoke-test step in `docker-publish.yml`.
- [x] **P2-02** Healthcheck + graceful shutdown (uvicorn `timeout_graceful_shutdown=5`; `docker stop` ~1 s in CI). *Verify:* Docker healthy; `docker stop` exits < 10 s.
- [~] **P2-03** **HUMAN** Unraid template `unraid/woosarr.xml`. Done: icon (`unraid/icon.png`), `WoosArr` owner URLs, `TemplateURL`, port 31337, `WOOSARR_LOG_LEVEL` option. *Remaining:* test install on an Unraid box.
- [x] **P2-04** Workflows: CI + gated publish + image smoke test + secret scan; `v0.1.0` tag published `0.1.0`, `0.1` and `latest` (2026-10-17). *Verify:* `gh run list`; `docker pull ghcr.io/woosarr/woosarr:0.1.0`.
- [x] **P2-05** `linux/arm64` re-enabled (QEMU; UI stage runs on `$BUILDPLATFORM`). The early "ARM" failures were really the `python-plexapi` PyPI name. First multi-arch publish green (`e09ed1c`). *Verify:* `docker buildx imagetools inspect ghcr.io/woosarr/woosarr:latest` lists amd64 + arm64.
- [x] **P2-06** GHCR package is public (anonymous manifest fetch → 200); README documents `docker run` and Unraid install.

### Phase 3 — Plex integration (all gated by P0-03)
- [x] **P3-00** `PlexApiGateway` passes the shared `GatewayContract` (fake plex.tv) and the live write check.
- [x] **P3-01** Plex OAuth endpoints (`POST /api/v1/plex/login`, `/login/poll`), token stored encrypted, 401 → "sign in again", disconnect signs the token out. *Verify:* `tests/test_integrations.py`, `tests/test_integrations_api.py`.
- [x] **P3-02** Server discovery (`GET /api/v1/plex/servers`, owned servers only selectable) and selection (`PUT /api/v1/plex/server`, machine id stored).
- [x] **P3-03** Library sync from plex.tv. **Live:** 15 libraries.
- [x] **P3-04** User sync from `account.users()` incl. Home users. **Live:** 63 users.
- [x] **P3-05** Per-user read/write by key; full reconciles batch-read every user's shares (one friends-list fetch + parallel share reads; live 63 users took 36 s before). **Live:** write check passed.
- [x] **P3-06** Retries with exponential backoff (reads on connection errors/timeouts/429/5xx; writes only on connection errors), connection state (`last_ok_at`, `last_error`, `auth_failed`) on the dashboard and Setup page.
- [x] **P3-07** All Plex calls run on the sync worker thread (`services/sync.py`), never the event loop; global run lock + per-user locks. *Verify:* `test_triggers_are_debounced_on_the_worker`; API stays responsive during runs (HTTP handlers never call Plex).

### Phase 4 — Discord integration (needs a bot token at runtime)
- [x] **P4-01** Bot lifecycle in the API event loop: start on boot, restart/stop from the UI (`POST /api/v1/discord/restart`, `DELETE /api/v1/discord`), token from encrypted secrets.
- [x] **P4-02** Guild selection (`GET /api/v1/discord/guilds`, `PUT /api/v1/discord/guild`); full role/member cache sync on connect (`services/discord_cache.py`).
- [x] **P4-03** Event handlers → per-user sync triggers: role changes, join, leave (raw), role create/update/delete. *Verify:* `DiscordCacheTests`. **LIVE** behaviour with a real guild is part of P11-04.
- [x] **P4-04** `/link` guild slash command: verified Plex sign-in or username modal, cooldown, audit, one-to-one rules, no token persisted. **Live:** both paths used successfully (username → access granted by roles; Plex sign-in → verified link).
- [x] **P4-05** discord.py reconnect/resume; `PrivilegedIntentsRequired`, `LoginFailure`, bot-not-in-guild and crashes surface as status details in the UI.
- [~] **P4-07** `/unlink` (self): confirmation, `unlink_policy` setting (same options as the leave-server policy), drop + unlink under one lock, audit. *Verify:* `tests/test_invite_unlink.py::UnlinkTests`. *Remaining:* **LIVE** try both policies.
- [~] **P4-08** `/invite email [member]`: self-invite for allowed roles, admins for anyone, libraries from roles (no exempt), refusals (dry-run, linked, pending, already on server, cooldown), `plex_invites` table (migration 0003), auto-link on acceptance, Users page list/cancel. *Verify:* `tests/test_invite_unlink.py::InviteTests`, gateway invite test. *Remaining:* **LIVE** send and accept a real invite.
- [~] **P4-09** Link-status roles (Verified/Unverified/Linked) kept in step after every sync; create from Settings. *Verify:* `tests/test_discord_features.py`. *Remaining:* **LIVE** with Manage Roles + hierarchy.
- [~] **P4-10** Per-library role creation (Libraries page) mapped to the library; name (prefilled `Plex · <library>`), colour and extra libraries editable in the shared create-role dialog. *Verify:* Playwright `libraries: create role lets the admin set the name`. *Remaining:* **LIVE**.
- [x] **P4-14** Remove roles deleted in Discord: `GET /api/v1/roles/deleted` (references), `DELETE /api/v1/roles/{id}` (409 if it still exists), `POST /api/v1/roles/remove-deleted`; cleans mappings, settings, reaction options, member caches; audited. *Verify:* `tests/test_roles_cleanup.py`, Playwright `roles deleted in Discord can be removed`.
- [~] **P4-11** Reaction posts (reaction roles): channel pick/create, post/edit, reactions, pin; reacting gives Discord roles, un-reacting removes them (kept if another reaction gives the same role); re-synced on reconnect (migration 0004). Libraries follow from role mappings. *Verify:* `tests/test_discord_features.py`. *Remaining:* **LIVE** post + react.
- [x] **P4-12** Bot permission feature gating + invite-link builder + `docs/discord-notes.md` breakdown.
- [~] **P4-13** Bot avatar from WooSarr: default WooSarr icon (packaged `assets/bot-avatar.png`) or admin upload (PNG/JPEG/GIF sniffed from bytes, ≥128 px, ≤8 MB), optional application icon, `PUT /api/v1/discord/avatar`, rate-limit message, audit; Setup → Discord → Bot avatar. *Verify:* `tests/test_discord_avatar.py` (local fake Discord API), Playwright `discord wizard…`. *Remaining:* **LIVE** set it on the real bot.
- [x] **P4-06** Admin link/unlink API (`PUT/DELETE /api/v1/users/{id}/link`, one-to-one enforced, audited, triggers reconcile). *Verify:* `test_link_unlink_and_sources`.

### Phase 5 — Sync engine
- [x] **P5-01** Pure calculator — `sync/engine.py`. *Verify:* `pytest tests/test_engine.py` (19 tests).
- [x] **P5-02** Apply layer with dry-run (`sync/apply.py`); every planned/applied/skipped change is written to `audit_log` (actor, add/remove, reasons, trigger, dry_run) and mirrored to the app log. *Verify:* `tests/test_sync_service.py`.
- [x] **P5-03** `SyncService.trigger()` with per-user debounce; Discord events and mapping/exemption/settings changes feed it.
- [x] **P5-04** Periodic full reconcile (`reconcile_interval_minutes`, first run 15 s after start) + `POST /api/v1/sync` (all or listed users) + `GET /api/v1/sync/preview`. *Verify:* `test_sync_now_and_members`.
- [x] **P5-05** Concurrency control (one run at a time, per-user locks, drift check before write) and idempotency. *Verify:* `test_second_run_is_a_no_op`.

### Phase 6 — Web UI shell, auth, theme
- [x] **P6-01** Local admin login (argon2id), server-side sessions (sha256-stored id, 12 h idle / 7 d absolute, rotated on login), CSRF header on unsafe methods, logout, per-account lockout (5 → 15 min), per-IP limiter (10 / 5 min), HttpOnly + SameSite=Lax cookie (Secure on HTTPS or `WOOSARR_COOKIE_SECURE=always`). **API keys** (`wsk_…`, `read` = GET only, `admin`) for scripted access (D16). API docs/OpenAPI require auth. *Verify:* `pytest tests/test_auth.py tests/test_app.py`.
- [x] **P6-02** App shell with sidebar (Dashboard, Users, Roles, Libraries, Logs, Settings, Setup); drawer below 860 px, skip link, focus-visible, focus-trapped modals. *Verify:* Playwright `sidebar navigation`, `mobile layout`.
- [x] **P6-03** Theme: light/dark (OS default + toggle), purple accent, red-green and blue-yellow palettes, extra-contrast option, stored per browser; statuses always icon + text (N8). *Verify:* `python3 scripts/check_contrast.py` (all 12 combinations ≥ 4.5:1, in CI); Playwright `theme` test.
- [x] **P6-04** Shared components in `src/ui.tsx`: badges, accessible `Field` (hint via aria-describedby), modal, confirm dialog, toasts, empty/loading/error states (with request id), responsive tables.
- [-] **P6-05** SAML SSO — deferred (D7a).
- [x] **P6-06** First-run admin creation: `POST /api/v1/setup/admin`, disabled forever once an admin exists, race-safe (process lock + PK claim row), needs the one-time setup code printed to **stdout only** (never the log file/API; `WOOSARR_SETUP_CODE_REQUIRED=false` to skip). Setup screen in the UI. *Verify:* `test_concurrent_claims_create_exactly_one_admin`; Playwright `first-run setup`.
- [x] **P6-07** Admin user management API + Settings → Admins UI (create/disable/delete/reset/unlock; last active admin protected; disable/reset ends sessions). *Verify:* `AdminManagementTests`.
- [x] **P6-08** OIDC SSO (D21): `services/oidc.py` (discovery, PKCE, state cookie, nonce, JWKS signature + iss/aud/azp/exp/iat checks, userinfo merge, access rules), presets `services/oidc_presets.py`, API `api/oidc.py`, migration 0006, login buttons, Settings → Single sign-on with a 5-step wizard per provider, test sign-in, lockout guards; docs `docs/sso.md` generated from the presets. *Verify:* `tests/test_oidc.py` (fake IdP with real RS256 keys: replay, nonce, audience, issuer, alg none/HS256, unknown key, expired, PKCE, rules, Entra issuer, Google hd), Playwright `sso wizard`. *Remaining:* **LIVE** against a real Okta/Entra/Google/ADFS tenant.

### Phase 7 — Feature pages
- [x] **P7-01** Users page: Plex users, linked Discord, roles, libraries, access source (role/manual/exempt/external); **Verified badge** (icon + text); admin link/**unlink** with confirmation; bulk manual add/remove; filter verified/unverified/unlinked. *Verify:* Playwright `admin links a user…`, `manual grant…`.
- [x] **P7-02** Roles page: assign libraries to roles with live impact preview ("N users gain/lose X"). *Verify:* Playwright + `test_role_mapping_preview_and_audit`.
- [x] **P7-03** Libraries page: live server card (name, PMS version, platform, online via `GET /api/v1/plex/server`), per-library exempt toggle with confirmation, referencing roles, share counts.
- [x] **P7-04** Dashboard: Plex/Discord status, last sync report, pending changes (preview), 24 h error count, dry-run banner, Sync now.
- [x] **P7-05** Settings: general (dry-run with go-live confirmation, interval, leave policy, ignore-unverified, cooldown, log level, audit retention), appearance, my account, admins, API keys. Secret replacement is replace-only on the Setup page (re-sign-in to Plex, *Replace token* for Discord). SAML deferred (D7a).

- [x] **P7-06** Customizable dashboard: per-admin layout (`GET/PUT/DELETE /api/v1/dashboard/layout`, move/resize/hide/add), widgets from `GET /api/v1/dashboard/insights` (link coverage, access changes per day, activity, library access, requests, invites, setup checklist, security) and number tiles. *Verify:* `tests/test_dashboard.py`, Playwright `dashboard widgets…` (+ axe in edit mode).
- [x] **P7-08** Dashboard: `xs` (eighth) size on an 8-column grid; per-widget `view` default / donut / bar with text legends. *Verify:* `test_layout_per_admin_with_defaults`, Playwright `dashboard widgets…` (+ axe with charts).
- [x] **P7-07** Users page *Linked* tab (`?link=linked`). *Verify:* `test_users_linked_filter`, Playwright `users page has a Linked tab…`.
- [x] **P7-09** Dashboard *Warnings and errors* widget: one compact line instead of the tall empty-state block when nothing is open. *Verify:* `make check`; dashboard with no open errors.
- [x] **P7-10** Sidebar/page names *Reaction Posts* and *Integrations* (route `/setup` kept). Discord feature checks say *Checking the Discord connection…* while loading and *Connecting to Discord…* (info, re-checked every 4 s) while the bot connects, instead of a false "isn't connected" warning. `GET /api/v1/discord/status` has a `connecting` flag (set up, no error, not ready) that the UI uses. *Verify:* `make check`, `make e2e` (sidebar test uses the new names), `test_status_connecting_until_ready_or_error`.
- [x] **P7-11** Dashboard: *Invites accepted this week* number tile; *Pending invites* is just the number (no sub-line or chart view); Plex invites widget drops the `/invite` hint. Bot `/help` (private reply listing `/link`, `/unlink`, `/invite`, the request command when Seerr is set up); `help` reserved for the request command name. *Verify:* `test_help_lists_every_command_privately`, `test_command_name_defaults_and_rules`, `make e2e`.
- [x] **P7-12** Dashboard *Discord role members* widget (like *Library access*): roles by member count with linked members, *No libraries* badge, circle/bar views; `roles` in `GET /api/v1/dashboard/insights` (bot-managed and deleted roles left out). Hidden by default; add it in edit mode. *Verify:* `tests/test_dashboard.py::DashboardApiTests::test_insights`, `make e2e`.
- [x] **P7-13** Typed-username (unverified) `/link` limited to roles: `username_link_all_members` (default on) + `username_link_role_ids`; members without a role only get *Sign in with Plex*, and `link_by_username` refuses them (`link_failed` reason `username_link_not_allowed`); `/link` refreshes the member's cached roles first. Settings split into General (sync, dry-run, logging) and Linking (username linking, cooldown, leave/unlink policies, link-status roles, *Plex Invites*) tabs, old `?tab=invites|logging` links redirected; `.settings` cards put each setting in its own band with a divider (floated fieldset legends). UI also reserves `/help`. *Verify:* `test_username_link_limited_to_roles`, Playwright `settings tabs: username linking limited to a role` (+ axe on the new tabs).
- [x] **P7-14** Lists sort case-insensitively (Plex users, Discord members, libraries, admins, SSO providers, dashboard library ties, unusable role names): `ORDER BY lower(x), x`. *Verify:* `tests/test_domain_api.py::DomainApiTests::test_lists_sort_case_insensitively`.
- [x] **P7-15** Dashboard counts the server owner as a Plex user everywhere (Link coverage matched *Plex users* minus one; Library access per-library users add the owner, who sees every library; friend shares exclude owner rows). Sync, manual grants and invite matching still skip the owner on purpose. Libraries page *Shared with* stays share-only. *Verify:* `tests/test_dashboard.py::DashboardApiTests::test_insights`.
- [x] **P8-07** Dashboard *Warnings and errors* renamed **Alerts**, no 24 h window (an alert stays until resolved). Account alerts from the audit log, naming everyone involved: refused attempts on a Plex account linked to someone else (`plex_account_taken`, or `discord_already_linked` with a holder; `link_failed` now records `holder_discord_user_id`, `method`), successful takeovers of an unverified link by Plex sign-in (`link_self` outcome `replaced`), and admin moves (`link_admin` with a different `previous_discord_user_id`). Warnings/errors ring refilled from the log files at startup so alerts survive restarts. Resolutions are kept while anything they hide exists. *Verify:* `test_account_takeover_alerts`, `test_group_resolve_hides_for_24h_and_unresolve`, `tests/test_logs.py::LoggingTests::test_warnings_survive_a_restart`.
- [x] **P8-08** `/config/logs/alerts.log`: WARNING+ only, JSON lines, redacted, 2 MB × 3 (`WOOSARR_ALERTS_FILE_MAX_BYTES`, `WOOSARR_ALERTS_FILE_BACKUPS`). Alerts reload from it at startup (from `woosarr.log` the first time, before it exists). `GET /api/v1/logs/file?name=alerts` downloads it. *Verify:* `tests/test_logs.py` (`test_warnings_survive_a_restart`, `test_first_start_with_alerts_file_reloads_from_main_log`, canary), `tests/test_app.py::…::test_log_file_download`.

### Phase 8 — Logs
- [x] **P8-01** Logs page: live tail (2 s cursor polling), level filter, search, export shown lines (JSONL) and full file; keeps up to 20k lines with `content-visibility` rows. Push streaming dropped: polling with `since` is enough for one admin UI.
- [x] **P8-02** Audit trail tab (`GET /api/v1/audit`, action filter, paging).
- [x] **P8-03** Log level setting applies live; file logs rotate by size (`WOOSARR_LOG_FILE_MAX_BYTES` × backups); audit retention in days, pruned on full reconcile.

- [x] **P8-06** Live error 2026-10-18 21:50: `on_guild_role_update` → UNIQUE constraint on `discord_roles_cache` (role-create + role-update for the new *Patreon* role handled in parallel threads). Role and member cache writes are atomic SQLite upserts. *Verify:* `tests/test_integrations.py::DiscordCacheTests::test_role_and_member_writes_survive_a_concurrent_insert` (reproduced the error before the fix).
- [x] **P8-04** Dashboard warnings/errors grouped by fingerprint (`services/error_digest.py`), **mark resolved** hides a group for 24 h (`PUT/DELETE /api/v1/errors/{fp}/resolved`, migration 0009, audited); separate WARNING+ ring buffer. *Verify:* `tests/test_dashboard.py`.
- [x] **P8-05** Live log review 2026-10-18: reaction-post publish 500 (missing channel permissions → pre-check + clear errors), `/link` Plex button "Unknown interaction" (defer before plex.tv), gateway reconnect noise (INFO + 60 s watchdog), 4004 message, Seerr 404 noise. *Verify:* `tests/test_discord_features.py::…publish_refused_up_front…`, `tests/test_discord_events.py::LiveLogFixTests`.

### Phase 9 — Setup assistants
- [x] **P9-01** Discord wizard on the Setup page. **Live:** bot *WooSarr* connected to *Dave's Plex Chat* (6 roles, 36 members).
- [x] **P9-02** Plex wizard: sign in → server → read check → write check. **Live:** done on *Bragi*.
- [x] **P9-03** First-run onboarding admin → Plex → Discord entirely in the UI. **Live:** done 2026-10-17.

### Phase 10 — Security
- [x] **P10-01** Secret scan: gitleaks over the full git history (CI + pre-commit), Trivy secret scan of the built image before publishing (fails the publish); zero findings (`e09ed1c`). *Verify:* CI `build` job.
- [x] **P10-02** Redaction audit: canaries through urllib3/requests/plexapi/aiohttp/discord.http/httpx debug logs, unregistered token shapes, tracebacks, the 500 handler, startup re-registration, API responses. *Verify:* `pytest tests/test_redaction_audit.py` (5 of 6 fail with redaction disabled; the 6th checks responses structurally).
- [x] **P10-03** Cookies, headers, CORS, input validation, auth rate limits. Done (live API audit 2026-10-17): CSP/nosniff/frame/referrer/COOP headers, cache policy (`no-store` API, immutable assets, `no-cache` HTML), validated `X-Request-ID`, no `Server` header, real 404 for missing static files, no CORS (same origin only). Cookie flags and auth rate limits in P6-01. Confirmed on the live server after deploy. *Verify:* `pytest tests/test_app.py tests/test_auth.py`; `curl -I http://10.0.1.101:31337/`.
- [x] **P10-04** README *HTTPS and reverse proxies* (`WOOSARR_FORWARDED_ALLOW_IPS`, `WOOSARR_COOKIE_SECURE`, Caddy example).
- [x] **P10-05** Non-root (gosu → PUID); `backend/constraints.txt` pins runtime deps (`make lock`; Dockerfile + CI install with `-c`); npm lockfile; CI runs `pip-audit` and `npm audit --omit=dev --audit-level=moderate` (`make audit`). react-router upgraded to 7.18.4 for GHSA-wrjc-x8rr-h8h6. The dev-only vite/esbuild advisory was resolved in P10-08 (vite 8).
- [x] **P10-06** Threat model `docs/threat-model.md`: unverified `/link` claims (accepted, mitigated by badge/one-to-one/cooldown/audit/unlink), role escalation, `/config` theft, SAML forgery/replay, first-run takeover.
- [x] **P10-08** Security assessment 2026-10-18 (auth/session/CSRF/API keys, public routes, OIDC redirects, SPA serving, Discord + Seerr input/output, dependencies, container). Fixed: untrusted TMDB text could inject masked links into Discord messages (`services/discord_text.py`), URL parts from Seerr validated, suggestions gated + per-member search limit, SSO `next` control characters, setup code length, `umask 027` + PUID=0 warning, dev toolchain advisories (vite 8 / vitest 5). Accepted: admin-entered Seerr URL can reach internal hosts. *Verify:* `tests/test_requests.py::UntrustedTextTests`, `test_search_rate_limit_per_member`, `tests/test_oidc.py::SafeNextTests`, `npm audit` (0), `pip-audit -r constraints.txt` (0).
- [x] **P10-07** Auth hardening: argon2id defaults (t=3, m=64 MiB, p=4, rehash on login), session rotation, lockout + IP limiter, console recovery (`python -m woosarr.admin`). SAML lockout guard not applicable (D7a).

### Phase 11 — Testing
- [x] **P11-01** Unit tests cover engine, apply, secrets, redaction, linking, config, logs, DB, auth, sync service, recovery (137 backend tests). Keep extending as code grows.
- [x] **P11-02** Integration tests over HTTP with demo data, the sync service against a fake plex.tv, and simulated Discord gateway events through the real bot handlers (`tests/test_discord_events.py`).
- [x] **P11-03** Playwright: `app` project (demo backend; every page, flows, axe, mobile) and `wizards` project (real app + fake plex.tv/Discord via `backend/tests/e2e_wizard_server.py`: Plex sign-in popup, server pick, read/write checks; Discord bad/good token, invite, server pick, axe). 11 tests, stable over repeated runs.
- [ ] **P11-04** **HUMAN** Live test on a real Plex test server + Discord guild; verify N3–N6.
- [~] **P11-05** Contrast checked for every theme combination; axe (WCAG 2.1 AA, serious/critical) runs on 10 pages × light/dark in Playwright and passes (fixed: tab ARIA, keyboard-focusable scroll regions, field hints). *Remaining:* **HUMAN** screen-reader pass.
- [~] **P11-06** **Live evidence:** the container was updated several times on the Unraid host with admin, sessions, secrets and settings kept (`/config` persistence). *Remaining:* **HUMAN** fresh install from the template.

### Phase 12 — Docs and release
- [x] **P12-01** README: install (Unraid, Docker), first-run walkthrough with setup code, HTTPS, troubleshooting, backups, FAQ.
- [x] **P12-02** `docs/admin-guide.md`: sync rules, triggers, leave policy, verified links, dry-run, troubleshooting, admins, env vars.
- [x] **P12-03** Backup/restore and master key in `docs/admin-guide.md`; startup survives an unreadable secret (logs an error, admin re-enters it). *Verify:* `tests/test_recovery.py`.
- [~] **P12-04** `CHANGELOG.md` written; `v0.1.0` released (P2-04). *Remaining:* v1.0 after P11-04 live verification; Community Apps submission (HUMAN, forum).

### Phase 13 — Requests
- [~] **P13-01** Seerr requests (D22): client `services/seerr.py` (strict query encoding, request-as-user, import from Plex), `services/media_requests.py` (Plex-id → email → username mapping, role/verified gating, submit, 5-min status poll + DMs), Discord flow `services/discord_requests.py` (autocomplete within 3 s, pick menu, details card, season select), command registered under `request_command_name` (default `request`; re-registered on rename or Seerr connect/disconnect), API `api/requests_api.py`, migration 0007, Setup card, Settings → Requests, Requests page, `docs/requests.md`. *Verify:* `pytest tests/test_requests.py` (fake Seerr from Seerr's source), Playwright `request command name…` + axe on the new pages. *Remaining:* **LIVE** connect a real Seerr, request from Discord, see it in Seerr under the member, get the DM.
- [~] **P13-04** Public requests: `request_visibility` private/public, `request_channel_id`, `request_channel_only`; post with member mention (only that user pingable), status replies + post status edit, DM fallback; migration 0008; feature `request_posts`. *Verify:* `tests/test_requests.py::PublicTests`, Playwright `request command name…`. *Remaining:* **LIVE** post in a real channel.
- [ ] **P13-02** Ombi as a second request provider.
- [ ] **P13-03** Audiobooks via Listenarr (research its API first).

## 10. Risks and open questions

- **R1** Plex share semantics (replace vs merge, last library, side effects, managed users) undocumented → P0-03 before any write code.
- **R2** Read-modify-write can race with direct Plex changes → per-user lock + re-read before write (done in `apply.py`).
- **R3** Discord Members intent is privileged (fine for one guild; document).
- **R4** Username-only links are unverified (accepted). Consider an "ignore unverified links" setting for v1.1.
- **R5** Master key sits beside the DB by default → document external key option.
- **R6/R7** python-plexapi is synchronous and community-maintained → keep behind `PlexGateway`, pin the version, contract tests.
- **Q1** Members who leave the server: default drops role-derived access, keeps manual + exempt.
- **Q2** Libraries mapped to no role are left alone ("unmanaged").
- **Q3** Notify users on Discord when access changes? Default no (v1.1).

## 11. Definition of done (v1.0)

- Fresh Unraid install → UI-only setup → working sync.
- N1–N10 verified by tests or documented checks.
- No secrets in repo, image, logs or API responses.
- Settings and secrets survive container update/restart.
- README, Unraid template and published image available.

## 12. References

- Plex Media Server API: https://developer.plex.tv/pms/ · SDK list: https://plexapi.dev/SDKs
- python-plexapi: https://python-plexapi.readthedocs.io/en/latest/ — key module https://python-plexapi.readthedocs.io/en/latest/modules/myplex.html (`MyPlexAccount.updateFriend`, `MyPlexUser`, `MyPlexServerShare`, `MyPlexPinLogin`, `MyPlexJWTLogin`)
- Discord developer docs: https://docs.discord.com/developers/intro
- Original spec: `docs/specsheet.md`
