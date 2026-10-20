# WooSarr — instructions for Claude Code

WooSarr syncs Plex library access from Discord roles. Single Docker container (Unraid), web UI on port 31337.

**Read `docs/action-plan.md` first.** It holds the status snapshot, decisions, non-negotiables, pitfalls and the task list. Work the next unblocked task, run its *Verify* command, update the plan in the same commit.

## Commands
- Everything: `make install && make check` (lint + tests, both halves); `make dev` runs the app on :31337
- Backend: `cd backend && pip install -e ".[dev]" && ruff check . && python -m pytest -q`
- Frontend: `cd frontend && npm install && npm run build`
- Image: `make image && make smoke` (needs Docker; CI runs the smoke test before publishing)
- Logs of a running instance: `curl "<host>:31337/api/v1/logs?level=WARNING"`
- CI: `gh run list --workflow "Docker publish" --limit 3`

## Rules that matter
- **Branches:** work on `develop` (the default branch; PRs and merges target it). Pushes to `develop` run CI and, if it passes, publish the `ghcr.io/woosarr/woosarr:develop` image (never `latest`). `main` is the release branch: merging `develop` into `main` publishes `latest`, a `v*` tag publishes that version. Only merge to `main` or tag when asked.
- Always verify (run `make check`) before committing. Commit per task, message `P5-01: short description`, then **always `git push`** (to `develop`). Never commit half-done work. Stage files by name, never `git add -A`: other sessions may be editing the same tree.
- There is no access to the deployed server. All diagnostics come from logs read through the API (`/api/v1/logs`), so new code must log enough (redacted) to be debugged that way.
- Never commit, log or print secrets/tokens. Never put a complete token-shaped string in source (GitHub push protection blocks it); assemble canaries at runtime as `tests/test_redaction.py` does.
- PyPI package for python-plexapi is **`PlexAPI`** (import `plexapi`).
- All Plex access goes through `PlexGateway`; write the complete library set, never a partial one.
- Stop and ask at the human gates listed in the plan (real Plex server, Discord bot, IdP, Unraid).
