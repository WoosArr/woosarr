# WooSarr — Action Plan (written for Claude Code)

> **Read this first.** You are an agent working in this repo. This file is the source of truth for scope, decisions, status and next steps. `CLAUDE.md` points here.
>
> **Operating loop**
> 1. Pick the first task in §9 that is `[ ]` or `[~]`, whose *Depends* are all `[x]`, and that is not marked **HUMAN** (see §8).
> 2. Do the work. Run the task's **Verify** command(s). Only when they pass, change the checkbox and the *Status snapshot* (§1) in the same commit.
> 3. Commit per task: `P5-01: short description`. **Do not `git push` unless the user asks** — pushes to `main` publish a Docker image (§3, D14).
> 4. If a decision is `OPEN` or a task is **HUMAN**-gated, stop and ask. Record answers in §3.
> 5. Keep task IDs stable. Add new tasks with the next free ID in the phase.
>
> Legend: `[x]` done and verified · `[~]` partly done (note says what remains) · `[ ]` todo · **HUMAN** needs a real Plex server / Discord bot / IdP / Unraid box / GitHub settings.

---

## 1. Status snapshot (2026-10-17)

- **Repo:** https://github.com/WoosArr/woosarr, branch `main`. Local clone: `~/Documents/GitHub/git-WoosArr/woosarr`.
- **Done and tested (49 stdlib unit tests):** sync engine, apply layer with dry-run, secrets manager, log redaction, link rules + one-shot token capture.
- **Written, not yet exercised:** FastAPI skeleton (`/api/health` + SPA serving), Vite/React placeholder, `PlexGateway` interface + fake, Unraid template.
- **CI/CD:** `docker-publish.yml` calls `ci.yml` first (lint, tests, frontend build, gitleaks), then builds `linux/amd64` and pushes to GHCR. An earlier ungated run published an image successfully. The gated pipeline's last known state: frontend and secret-scan passed; backend failed `ruff` (BLE001 in `core/redaction.py`) and the fix was awaiting push; pytest has **not yet run in CI**. Re-check with `gh run list --workflow "Docker publish" --limit 3`.
- **Not started:** every Plex-write implementation, Discord bot, DB layer, auth/wizard/SAML, the entire UI beyond a placeholder.
- **First thing to do in a fresh session:** run the baseline commands in §2 and fix anything red before new work.

## 2. Commands

```bash
# Backend (CI uses Python 3.12)
cd backend && pip install -e ".[dev]" && ruff check . && python -m pytest -q
# Frontend
cd frontend && npm install && npm run build
# Image (from repo root) and smoke test
docker build -f docker/Dockerfile -t woosarr:dev .
docker run --rm -p 2000:2000 -v "$PWD/config:/config" woosarr:dev   # config/ is git-ignored
curl -fsS localhost:2000/api/health
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
| D14 | CI/CD | DECIDED | GitHub Actions. `docker-publish.yml` = CI (reusable `ci.yml`) → build → push to GHCR on `main` and `v*.*.*` tags, `GITHUB_TOKEN` only. PRs run CI only. `linux/amd64` only for now. |

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
- **N10** Every web route requires auth except health, login, SAML metadata/ACS, and the first-run wizard (only while no admin exists).

## 5. Architecture

```
Container (port 2000)
├── FastAPI: /api/* (REST), / (built React bundle), auth/session middleware
├── PlexGateway (python-plexapi impl + FakePlexGateway)
├── Discord service (discord.py) in the same event loop
├── Sync engine (pure) + apply layer ← backend/woosarr/sync/
├── Scheduler (periodic reconcile), Secrets manager, redacting logger
└── SQLite /config/app.db (+ /config/secrets/master.key)
```

Current layout (planned items in *italics*):
```
backend/pyproject.toml
backend/woosarr/{__init__,main,linking,plex_gateway}.py
backend/woosarr/core/{secrets,redaction}.py      + config, logging, db, auth (planned)
backend/woosarr/sync/{engine,apply}.py           + scheduler (planned)
backend/tests/                                   (unittest-style, run by pytest)
frontend/{package.json,vite.config.ts,src/main.tsx}
docker/{Dockerfile,entrypoint.sh}   unraid/woosarr.xml
.github/workflows/{ci,docker-publish}.yml   .gitleaks.toml
docs/{action-plan,specsheet}.md   CLAUDE.md   README.md   LICENSE
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
- **gitleaks action needs a licence on org repos**; CI runs the `zricethezav/gitleaks` container instead.
- `docker/Dockerfile` copies `frontend/package-lock.json*` optionally; commit a lockfile once `npm install` has produced one (then CI uses `npm ci`).
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
- [ ] **P0-06** Unraid template requirements (appdata, port, icon, WebUI). *Verify:* `docs/unraid-notes.md`.
- [ ] **P0-07** **HUMAN** Plex token invalidation for `/link` OAuth (N9): after reading account info with the temporary token, invalidate it and prove it no longer works. *Verify:* notes + test in `docs/plex-notes.md`.
- [ ] **P0-08** **HUMAN** SAML library choice and flow (SP-initiated, metadata, signature validation, clock skew, replay) against a test IdP. *Verify:* `docs/saml-notes.md`; resolves D7a.

### Phase 1 — Foundation
- [~] **P1-01** Tooling. Done: `.gitignore`, `.gitattributes`, `.dockerignore`, `pyproject.toml`, CI. *Remaining:* pre-commit (ruff + gitleaks), vitest, a `Makefile` or `scripts/` wrapper for §2 commands. *Verify:* one command runs lint + tests for both halves.
- [ ] **P1-02** Config loader (env + `/config`, no secrets in defaults). *Depends:* P1-01. *Verify:* app boots with only a `/config` volume.
- [ ] **P1-03** SQLAlchemy models + Alembic migrations for §5 data model. *Verify:* fresh DB migrates; migration test passes.
- [x] **P1-04** Secrets manager — `core/secrets.py`. *Verify:* `pytest tests/test_secrets.py`.
- [~] **P1-05** Logging. Done: redaction (`core/redaction.py`, tested). *Remaining:* structured logging setup, install `RedactingFilter` on all handlers, DB + rotating-file persistence, streaming. *Verify:* canary secrets never appear in any sink.
- [~] **P1-06** FastAPI app. Done: skeleton in `main.py`. *Remaining:* run it for real, error handling, API versioning, sessions. *Verify:* `curl localhost:2000/api/health` → 200 in the container.
- [ ] **P1-07** OpenAPI → TypeScript type pipeline into `frontend/src/generated/` (git-ignored; generated in build/CI). *Verify:* changing a response model breaks the frontend build until types regenerate.

### Phase 2 — Container and CI/CD
- [~] **P2-01** Dockerfile + entrypoint (PUID/PGID, gosu, healthcheck). CI builds it (amd64). *Remaining:* run the image locally and confirm `/api/health`, healthy status, `/config` persistence, graceful SIGTERM. *Verify:* §2 smoke test.
- [~] **P2-02** Healthcheck + graceful shutdown. *Verify:* `docker ps` healthy; `docker stop` exits < 10 s.
- [~] **P2-03** **HUMAN** Unraid template `unraid/woosarr.xml`. *Remaining:* icon, replace `OWNER` placeholders with `WoosArr`, test install.
- [~] **P2-04** Workflows. Done: CI + gated publish; first publish succeeded. *Remaining:* confirm the gated run goes green end-to-end; test a `v0.1.0` tag produces semver tags. *Verify:* `gh run list`; `docker pull ghcr.io/woosarr/woosarr:latest`.
- [ ] **P2-05** Re-enable `linux/arm64` (QEMU) once the amd64 pipeline is stable; fix any arm-only dependency problem. *Verify:* multi-arch manifest.
- [ ] **P2-06** **HUMAN** Make the GHCR package public; document `docker run` / Unraid install in README. *Verify:* anonymous `docker pull` works.

### Phase 3 — Plex integration (all gated by P0-03)
- [~] **P3-00** `PlexGateway` interface + `FakePlexGateway` done. *Remaining:* python-plexapi implementation (`plex_gateway_plexapi.py`) — write only after P0-03. *Verify:* shared contract tests run against both the fake and (opt-in, env-gated) the real implementation.
- [ ] **P3-01** Plex OAuth login endpoints (start/poll), token stored encrypted, expiry/revocation handling. *Depends:* P0-02, P1-04.
- [ ] **P3-02** Server discovery/selection; store machine identifier.
- [ ] **P3-03** Library sync (key, title, type; handle renames/removals).
- [ ] **P3-04** User sync (incl. managed/home users).
- [ ] **P3-05** Per-user read/write: read `MyPlexUser.servers[].sections()` filtered on `shared`; write via `updateFriend(sections=full_set)`; `removeSections=True` only when the target is empty; preserve sync/channel/filter settings per P0-03(d,e). *Verify:* add one library without losing others; remove one; remove the last; settings unchanged.
- [ ] **P3-06** Retries, backoff, rate limits, connection-lost state surfaced to UI.
- [ ] **P3-07** Run all Plex calls in a thread pool with per-user serialization. *Verify:* a slow Plex call doesn't stall API or Discord heartbeat.

### Phase 4 — Discord integration (needs a bot token at runtime)
- [ ] **P4-01** Bot lifecycle (start/stop/restart without container restart; token from secrets).
- [ ] **P4-02** Guild selection; cache roles and members.
- [ ] **P4-03** Event handlers (member role change, role update/delete, join/leave) → sync queue.
- [~] **P4-04** `/link`. Done: `linking.py` rules + `capture_identity_once` (always invalidates), tested. *Remaining:* slash command, username modal, OAuth button/poll, persistence, cooldown, audit. *Verify:* both paths work; test proves no token is persisted anywhere (N9).
- [ ] **P4-05** Reconnect/resume; permission and intent errors surfaced in UI.
- [ ] **P4-06** Admin link/unlink API (unlink triggers reconcile; audited).

### Phase 5 — Sync engine
- [x] **P5-01** Pure calculator — `sync/engine.py`. *Verify:* `pytest tests/test_engine.py` (19 tests).
- [~] **P5-02** Apply layer with dry-run done (`sync/apply.py`, tested). *Remaining:* persisted audit log (who/what/why/dry_run).
- [ ] **P5-03** Event-driven triggers with per-user debounce; mapping changes reconcile all holders.
- [ ] **P5-04** Periodic full reconcile + "Sync now" (user/global).
- [ ] **P5-05** Concurrency control and idempotency (running twice = no changes the second time).

### Phase 6 — Web UI shell, auth, theme
- [ ] **P6-01** Local admin login (argon2id), server-side sessions, CSRF, logout, lockout/rate limit. *Verify:* unauthenticated requests rejected on all routes except those in N10.
- [ ] **P6-02** App shell with sidebar (Dashboard, Users, Roles, Libraries, Logs, Settings, Setup); responsive, keyboard accessible.
- [ ] **P6-03** Theme system: light/dark (OS + toggle), purple accent, colorblind palettes, WCAG AA contrast, state persisted.
- [ ] **P6-04** Shared components: tables, modals, toasts, confirm dialogs, badges, empty states.
- [ ] **P6-05** **HUMAN (needs IdP)** SAML SSO: SP metadata, SP-initiated login + ACS, signature/audience/skew validation, attribute and group mapping, SAML-only toggle with lockout guard. *Verify:* works with a test IdP; unsigned/replayed/expired assertions rejected.
- [ ] **P6-06** First-run admin wizard: create first admin; disabled forever once an admin exists; race-safe; optional setup token printed to container logs. *Verify:* concurrent second claim fails.
- [ ] **P6-07** Admin user management (create/disable/delete/reset; cannot remove the last active admin).

### Phase 7 — Feature pages
- [ ] **P7-01** Users page: Plex users, linked Discord, roles, libraries, access source (role/manual/exempt/external); **Verified badge** (icon + text); admin link/**unlink** with confirmation; bulk manual add/remove; filter verified/unverified/unlinked.
- [ ] **P7-02** Roles page: assign libraries to roles with impact preview ("N users gain/lose X").
- [ ] **P7-03** Libraries page: server info, per-library **exempt** toggle with explanation, referencing roles.
- [ ] **P7-04** Dashboard: Plex/Discord status, last sync, errors, dry-run indicator.
- [ ] **P7-05** Settings: theme, reconcile interval, dry-run, leave-server policy, secret rotation (replace-only), log retention, admin users, SAML.

### Phase 8 — Logs
- [ ] **P8-01** Logs page: live tail, filters, search, export; handles 10k+ lines.
- [ ] **P8-02** Audit view of permission changes.
- [ ] **P8-03** Log level control and retention.

### Phase 9 — Setup assistants
- [ ] **P9-01** **HUMAN** Discord bot creation wizard (app, bot, Members intent, token, invite URL, guild); validates live.
- [ ] **P9-02** **HUMAN** Plex wizard (OAuth, server pick, library fetch, share-read test).
- [ ] **P9-03** First-run onboarding: admin (P6-06) → Plex → Discord, gating the rest of the UI. *Verify:* fresh container → working system via UI only.

### Phase 10 — Security
- [ ] **P10-01** Secret scan of repo, image and history (gitleaks, already in CI) — zero findings.
- [ ] **P10-02** Redaction audit across exception traces and HTTP client debug logs (canary tests).
- [ ] **P10-03** Cookies, headers, CORS, input validation, auth rate limits.
- [ ] **P10-04** README guidance: reverse proxy/HTTPS (port 2000 is plain HTTP).
- [ ] **P10-05** Non-root container, dependency audit (`pip-audit`, `npm audit`), pin versions.
- [ ] **P10-06** Threat model `docs/threat-model.md`: unverified `/link` claims (accepted, mitigated by badge/one-to-one/cooldown/audit/unlink), role escalation, `/config` theft, SAML forgery/replay, first-run takeover.
- [ ] **P10-07** Auth hardening: argon2 parameters, session rotation, SAML-only lockout guard, brute-force protection.

### Phase 11 — Testing
- [ ] **P11-01** Keep unit coverage on engine, secrets, redaction, linking (exists; extend as code grows).
- [ ] **P11-02** Integration tests with the fake Plex and a mocked Discord.
- [ ] **P11-03** Playwright UI tests (wizards, mappings, exemptions, manual grants, link/unlink).
- [ ] **P11-04** **HUMAN** Live test on a real Plex test server + Discord guild; verify N3–N6.
- [ ] **P11-05** Accessibility and theme review.
- [ ] **P11-06** **HUMAN** Unraid install/upgrade/restart/persistence test.

### Phase 12 — Docs and release
- [ ] **P12-01** README: install on Unraid, first-run walkthrough, FAQ.
- [ ] **P12-02** Admin docs for sync rules (additive, exemptions, manual grants, external shares, verified links).
- [ ] **P12-03** Backup/restore and master-key handling (losing `master.key` = re-enter secrets).
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
