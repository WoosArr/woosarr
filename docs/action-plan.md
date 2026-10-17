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
> Legend: `[x]` done and verified · `[~]` partly done (note says what remains) · `[ ]` todo · **HUMAN** needs a real Plex server / Discord bot / IdP / Unraid box / GitHub settings.

---

## 1. Status snapshot (2026-10-17)

- **Repo:** https://github.com/WoosArr/woosarr, branch `main`. Local clone: `~/Documents/GitHub/git-WoosArr/woosarr`.
- **Port:** **31337** (D15; was 2000).
- **Done and tested (71 backend tests + 6 opt-in real-Plex contract tests skipped, 2 vitest):** sync engine, apply layer with dry-run, secrets manager, log redaction, link rules + one-shot token capture, config loader, logging (stderr JSON + rotating file + in-memory buffer, all redacted), FastAPI app factory (`/api/v1`, error handler with request ids, SPA serving), **logs API** (`GET /api/v1/logs`, `GET /api/v1/logs/file`), `PlexGateway` contract tests.
- **Ran for real locally:** `python -m woosarr` serves health, UI, logs on :31337 and stops cleanly on SIGTERM (~0.2 s).
- **CI/CD:** `docker-publish.yml` calls `ci.yml` (ruff, pytest, vitest, frontend build, gitleaks), then builds `linux/amd64`, **smoke-tests the image** (`scripts/smoke-test.sh`: health, Docker healthy, `/config` persistence across restart, `docker stop` < 10 s) and only then pushes to GHCR. Gated runs green through `d68c5e8`, including the image smoke test; that image is published as `latest`. No Docker on the dev Mac, so the image is only exercised in CI.
- **Diagnostics:** nobody has shell access to the deployed server (D16, http://10.0.1.101:31337). Logs come from `GET /api/v1/logs` (since/level/q/limit) and `GET /api/v1/logs/file`, which need an admin session or a `read` API key (`Authorization: Bearer wsk_…`). Claude reads the key from `~/.config/woosarr/api-key` and never prints it.
- **Not started:** every Plex-write implementation, Discord bot, DB layer, auth/wizard/SAML, the entire UI beyond a placeholder.
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
| D7a | SAML library | OPEN (default) | `python3-saml` or `pysaml2`; choose in P0-08. |
| D8 | Triggers | DECIDED | Discord gateway events + periodic full reconcile (default 15 min, configurable). |
| D9 | Library id | DECIDED | Plex section key + server machine identifier. |
| D10 | Name | DECIDED | **WooSarr** (image `ghcr.io/woosarr/woosarr`, package `woosarr`, appdata `/mnt/user/appdata/woosarr`). |
| D11 | Scope | DECIDED | One Discord guild, one Plex server in v1. |
| D12 | Dry-run | DECIDED | Global toggle; logs intended changes only. |
| D13 | Source control | DECIDED | Git, branch `main`, GitHub. |
| D14 | CI/CD | DECIDED | GitHub Actions. `docker-publish.yml` = CI (reusable `ci.yml`) → build → image smoke test → push to GHCR on `main` and `v*.*.*` tags, `GITHUB_TOKEN` only. PRs run CI only. `linux/amd64` only for now. Every commit is pushed (user rule). |
| D15 | Port | DECIDED | **31337** (container and default host port). Overrides the 2000 in `docs/specsheet.md`, which is kept read-only as the original spec. Set by `WOOSARR_PORT`. |
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
| Real Plex server + test account(s) | Server owner signs in via OAuth (no tokens in repo; spike script reads env vars) | P0-02, P0-03, P0-07, P3-*, P11-04 |
| Discord application/bot + test guild | Bot token entered via the app or env var at runtime | P0-05, P4-*, P9-01 |
| SAML IdP (e.g. Keycloak/samltest) | IdP metadata | P0-08, P6-05 |
| GitHub settings | Package visibility (public), Actions permissions, branch protection | P2-06 |
| Unraid box | Template install test | P2-03, P11-06 |

## 9. Tasks

### Phase 0 — Discovery
- [x] **P0-00** Git repo, `.gitignore`, LICENSE, README, pushed to GitHub. *Verify:* `git ls-remote origin main`.
- [x] **P0-01** Decisions D4, D7, D10 resolved (§3).
- [ ] **P0-02** **HUMAN** Verify Plex auth with python-plexapi: `MyPlexPinLogin(oauth=True)` (`oauthUrl`, polling) and `MyPlexJWTLogin` (needs `pyjwt[crypto]`; check the minimum PlexAPI version that has it and bump `pyproject.toml`). Decide what the stored admin session is. *Verify:* notes + working example in `docs/plex-notes.md`.
- [ ] **P0-03** **HUMAN, gate for all Plex write code.** Write `scripts/spike_plex_share.py` (reads `PLEX_TOKEN`/`PLEX_SERVER` from env; never commit them). Answer in `docs/plex-notes.md`: (a) does `updateFriend(sections=…)` replace or merge? (b) does `MyPlexServerShare.sections()` return only shared sections or all with a `shared` flag? (c) how to revoke the *last* library? (d) does updating sections reset `allowSync/allowChannels/allowCameraUpload` or filters? (e) does it work for Home/managed users? (f) how to map `LibrarySection` ↔ shared `Section.key` and get `machineIdentifier`? *Verify:* spike proves read-then-write preserves existing access.
- [x] **P0-04** SDK chosen: python-plexapi (PyPI `PlexAPI`).
- [ ] **P0-05** **HUMAN** Discord facts: intents (Guild Members is privileged), permissions, slash command registration, ephemeral replies, modal + buttons for `/link`. *Verify:* `docs/discord-notes.md`.
- [x] **P0-06** Unraid template requirements (appdata, port, icon, WebUI). *Verify:* `docs/unraid-notes.md`.
- [ ] **P0-07** **HUMAN** Plex token invalidation for `/link` OAuth (N9): after reading account info with the temporary token, invalidate it and prove it no longer works. *Verify:* notes + test in `docs/plex-notes.md`.
- [ ] **P0-08** **HUMAN** SAML library choice and flow (SP-initiated, metadata, signature validation, clock skew, replay) against a test IdP. *Verify:* `docs/saml-notes.md`; resolves D7a.

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
- [~] **P2-04** Workflows. Done: CI + gated publish + image smoke test; green end-to-end (`d68c5e8`). *Remaining:* push a `v0.1.0` tag (ask first, it publishes a release image) and confirm semver tags. *Verify:* `gh run list`; `docker pull ghcr.io/woosarr/woosarr:0.1.0`.
- [~] **P2-05** `linux/arm64` re-enabled (QEMU; UI stage runs on `$BUILDPLATFORM`). The early "ARM" failures were really the `python-plexapi` PyPI name. *Remaining:* first green multi-arch publish. *Verify:* `docker buildx imagetools inspect ghcr.io/woosarr/woosarr:latest` lists amd64 + arm64.
- [ ] **P2-06** **HUMAN** Make the GHCR package public; document `docker run` / Unraid install in README. *Verify:* anonymous `docker pull` works.

### Phase 3 — Plex integration (all gated by P0-03)
- [~] **P3-00** `PlexGateway` interface + `FakePlexGateway` done; shared contract tests in `tests/test_plex_contract.py` (replace-not-merge, round trip, remove last library, idempotent) pass against the fake; real run is opt-in via `WOOSARR_PLEX_CONTRACT=1`. *Remaining:* python-plexapi implementation (`plex_gateway_plexapi.py`) — write only after P0-03 — and wire it into the contract test. *Verify:* contract tests pass against both.
- [ ] **P3-01** Plex OAuth login endpoints (start/poll), token stored encrypted, expiry/revocation handling. *Depends:* P0-02, P1-04.
- [ ] **P3-02** Server discovery/selection; store machine identifier.
- [~] **P3-03** Library sync (key, title, type; renames; removals flagged, not deleted) in `services/inventory.py`, tested on the fake. *Remaining:* run against the real gateway (P3-00).
- [~] **P3-04** User sync (incl. `is_managed`) in `services/inventory.py`, tested on the fake. *Remaining:* real gateway, confirm Home/managed users per P0-03(e).
- [ ] **P3-05** Per-user read/write: read `MyPlexUser.servers[].sections()` filtered on `shared`; write via `updateFriend(sections=full_set)`; `removeSections=True` only when the target is empty; preserve sync/channel/filter settings per P0-03(d,e). *Verify:* add one library without losing others; remove one; remove the last; settings unchanged.
- [ ] **P3-06** Retries, backoff, rate limits, connection-lost state surfaced to UI.
- [x] **P3-07** All Plex calls run on the sync worker thread (`services/sync.py`), never the event loop; global run lock + per-user locks. *Verify:* `test_triggers_are_debounced_on_the_worker`; API stays responsive during runs (HTTP handlers never call Plex).

### Phase 4 — Discord integration (needs a bot token at runtime)
- [ ] **P4-01** Bot lifecycle (start/stop/restart without container restart; token from secrets).
- [ ] **P4-02** Guild selection; cache roles and members.
- [ ] **P4-03** Event handlers (member role change, role update/delete, join/leave) → sync queue.
- [~] **P4-04** `/link`. Done: `linking.py` rules + `capture_identity_once` (always invalidates), tested. *Remaining:* slash command, username modal, OAuth button/poll, persistence, cooldown, audit. *Verify:* both paths work; test proves no token is persisted anywhere (N9).
- [ ] **P4-05** Reconnect/resume; permission and intent errors surfaced in UI.
- [x] **P4-06** Admin link/unlink API (`PUT/DELETE /api/v1/users/{id}/link`, one-to-one enforced, audited, triggers reconcile). *Verify:* `test_link_unlink_and_sources`.

### Phase 5 — Sync engine
- [x] **P5-01** Pure calculator — `sync/engine.py`. *Verify:* `pytest tests/test_engine.py` (19 tests).
- [x] **P5-02** Apply layer with dry-run (`sync/apply.py`); every planned/applied/skipped change is written to `audit_log` (actor, add/remove, reasons, trigger, dry_run) and mirrored to the app log. *Verify:* `tests/test_sync_service.py`.
- [~] **P5-03** `SyncService.trigger()` with per-user debounce (5 s); mapping/exemption/settings changes queue a full reconcile. *Remaining:* hook Discord gateway events (P4-03). *Verify:* `test_triggers_are_debounced_on_the_worker`.
- [x] **P5-04** Periodic full reconcile (`reconcile_interval_minutes`, first run 15 s after start) + `POST /api/v1/sync` (all or listed users) + `GET /api/v1/sync/preview`. *Verify:* `test_sync_now_and_members`.
- [x] **P5-05** Concurrency control (one run at a time, per-user locks, drift check before write) and idempotency. *Verify:* `test_second_run_is_a_no_op`.

### Phase 6 — Web UI shell, auth, theme
- [x] **P6-01** Local admin login (argon2id), server-side sessions (sha256-stored id, 12 h idle / 7 d absolute, rotated on login), CSRF header on unsafe methods, logout, per-account lockout (5 → 15 min), per-IP limiter (10 / 5 min), HttpOnly + SameSite=Lax cookie (Secure on HTTPS or `WOOSARR_COOKIE_SECURE=always`). **API keys** (`wsk_…`, `read` = GET only, `admin`) for scripted access (D16). API docs/OpenAPI require auth. *Verify:* `pytest tests/test_auth.py tests/test_app.py`.
- [x] **P6-02** App shell with sidebar (Dashboard, Users, Roles, Libraries, Logs, Settings, Setup); drawer below 860 px, skip link, focus-visible, focus-trapped modals. *Verify:* Playwright `sidebar navigation`, `mobile layout`.
- [x] **P6-03** Theme: light/dark (OS default + toggle), purple accent, red-green and blue-yellow palettes, extra-contrast option, stored per browser; statuses always icon + text (N8). *Verify:* `python3 scripts/check_contrast.py` (all 12 combinations ≥ 4.5:1, in CI); Playwright `theme` test.
- [x] **P6-04** Shared components in `src/ui.tsx`: badges, accessible `Field` (hint via aria-describedby), modal, confirm dialog, toasts, empty/loading/error states (with request id), responsive tables.
- [ ] **P6-05** **HUMAN (needs IdP)** SAML SSO: SP metadata, SP-initiated login + ACS, signature/audience/skew validation, attribute and group mapping, SAML-only toggle with lockout guard. *Verify:* works with a test IdP; unsigned/replayed/expired assertions rejected.
- [~] **P6-06** First-run admin creation: `POST /api/v1/setup/admin`, disabled forever once an admin exists, race-safe (process lock + PK claim row), needs the one-time setup code printed to **stdout only** (never the log file/API; `WOOSARR_SETUP_CODE_REQUIRED=false` to skip). *Remaining:* wizard UI (P9-03). *Verify:* `test_concurrent_claims_create_exactly_one_admin`.
- [~] **P6-07** Admin user management API (create/disable/delete/reset/unlock; last active admin protected; disable/reset ends sessions). *Remaining:* Settings UI. *Verify:* `AdminManagementTests`.

### Phase 7 — Feature pages
- [x] **P7-01** Users page: Plex users, linked Discord, roles, libraries, access source (role/manual/exempt/external); **Verified badge** (icon + text); admin link/**unlink** with confirmation; bulk manual add/remove; filter verified/unverified/unlinked. *Verify:* Playwright `admin links a user…`, `manual grant…`.
- [x] **P7-02** Roles page: assign libraries to roles with live impact preview ("N users gain/lose X"). *Verify:* Playwright + `test_role_mapping_preview_and_audit`.
- [~] **P7-03** Libraries page: per-library **exempt** toggle with confirmation and explanation, referencing roles, share counts. *Remaining:* server name/version once the real gateway exists.
- [x] **P7-04** Dashboard: Plex/Discord status, last sync report, pending changes (preview), 24 h error count, dry-run banner, Sync now.
- [~] **P7-05** Settings: general (dry-run with go-live confirmation, interval, leave policy, ignore-unverified, cooldown, log level, audit retention), appearance, my account, admins, API keys. *Remaining:* secret replace UI (with P9 wizards), SAML (P6-05).

### Phase 8 — Logs
- [x] **P8-01** Logs page: live tail (2 s cursor polling), level filter, search, export shown lines (JSONL) and full file; keeps up to 20k lines with `content-visibility` rows. Push streaming dropped: polling with `since` is enough for one admin UI.
- [x] **P8-02** Audit trail tab (`GET /api/v1/audit`, action filter, paging).
- [x] **P8-03** Log level setting applies live; file logs rotate by size (`WOOSARR_LOG_FILE_MAX_BYTES` × backups); audit retention in days, pruned on full reconcile.

### Phase 9 — Setup assistants
- [ ] **P9-01** **HUMAN** Discord bot creation wizard (app, bot, Members intent, token, invite URL, guild); validates live.
- [ ] **P9-02** **HUMAN** Plex wizard (OAuth, server pick, library fetch, share-read test).
- [~] **P9-03** First-run onboarding: admin step done (setup screen gates the whole UI; Setup page explains Plex/Discord steps). *Remaining:* Plex and Discord steps (P9-01/02). *Verify:* fresh container → working system via UI only.

### Phase 10 — Security
- [~] **P10-01** Secret scan: gitleaks over the full git history (CI + pre-commit), Trivy secret scan of the built image before publishing (fails the publish). *Remaining:* first green Trivy run. *Verify:* CI `build` job.
- [x] **P10-02** Redaction audit: canaries through urllib3/requests/plexapi/aiohttp/discord.http/httpx debug logs, unregistered token shapes, tracebacks, the 500 handler, startup re-registration, API responses. *Verify:* `pytest tests/test_redaction_audit.py` (5 of 6 fail with redaction disabled; the 6th checks responses structurally).
- [x] **P10-03** Cookies, headers, CORS, input validation, auth rate limits. Done (live API audit 2026-10-17): CSP/nosniff/frame/referrer/COOP headers, cache policy (`no-store` API, immutable assets, `no-cache` HTML), validated `X-Request-ID`, no `Server` header, real 404 for missing static files, no CORS (same origin only). Cookie flags and auth rate limits in P6-01. Confirmed on the live server after deploy. *Verify:* `pytest tests/test_app.py tests/test_auth.py`; `curl -I http://10.0.1.101:31337/`.
- [x] **P10-04** README *HTTPS and reverse proxies* (`WOOSARR_FORWARDED_ALLOW_IPS`, `WOOSARR_COOKIE_SECURE`, Caddy example).
- [x] **P10-05** Non-root (gosu → PUID); `backend/constraints.txt` pins runtime deps (`make lock`; Dockerfile + CI install with `-c`); npm lockfile; CI runs `pip-audit` and `npm audit --omit=dev --audit-level=moderate` (`make audit`). react-router upgraded to 7.18.4 for GHSA-wrjc-x8rr-h8h6. Accepted: dev-only vite/esbuild advisory (dev server, not in the image).
- [x] **P10-06** Threat model `docs/threat-model.md`: unverified `/link` claims (accepted, mitigated by badge/one-to-one/cooldown/audit/unlink), role escalation, `/config` theft, SAML forgery/replay, first-run takeover.
- [~] **P10-07** Auth hardening: argon2id defaults (t=3, m=64 MiB, p=4, rehash on login), session rotation, lockout + IP limiter, console recovery (`python -m woosarr.admin`). *Remaining:* SAML-only lockout guard (P6-05).

### Phase 11 — Testing
- [ ] **P11-01** Keep unit coverage on engine, secrets, redaction, linking (exists; extend as code grows).
- [~] **P11-02** Integration tests over HTTP with demo data (`WOOSARR_DEMO=true`: fake Plex + seeded Discord cache) in `tests/test_domain_api.py`, `tests/test_sync_service.py`. *Remaining:* mocked Discord *events* once P4-03 exists.
- [~] **P11-03** Playwright UI tests (`frontend/e2e`, `make e2e`, CI job `e2e`) against a demo backend: first-run setup, navigation, link/unlink, role mapping + impact, manual grants, exemptions, logs/audit, theme/palette, API key, go-live, mobile drawer; fails on any console error/CSP violation. *Remaining:* Plex/Discord wizards when they exist.
- [ ] **P11-04** **HUMAN** Live test on a real Plex test server + Discord guild; verify N3–N6.
- [~] **P11-05** Contrast checked for every theme combination; labels/hints/ARIA reviewed while fixing Playwright findings. *Remaining:* screen-reader pass (HUMAN) and an axe run.
- [ ] **P11-06** **HUMAN** Unraid install/upgrade/restart/persistence test.

### Phase 12 — Docs and release
- [x] **P12-01** README: install (Unraid, Docker), first-run walkthrough with setup code, HTTPS, troubleshooting, backups, FAQ.
- [x] **P12-02** `docs/admin-guide.md`: sync rules, triggers, leave policy, verified links, dry-run, troubleshooting, admins, env vars.
- [x] **P12-03** Backup/restore and master key in `docs/admin-guide.md`; startup survives an unreadable secret (logs an error, admin re-enters it). *Verify:* `tests/test_recovery.py`.
- [ ] **P12-04** v1.0 release: changelog, tag, image, Community Apps submission.

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
