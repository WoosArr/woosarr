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
- **Branches:** `develop` is the integration branch, `main` the release branch. Never commit or push directly to `main`, even though `main` is GitHub's default branch (what visitors and the Unraid template see). Pushes to `develop` (including merged PRs) run CI and, if it passes, publish the `ghcr.io/woosarr/woosarr:develop` image (never `latest`). Merging `develop` into `main` runs CI but publishes nothing; the `vX.Y.Z` tag on the merge commit publishes that version and, if it's the newest release, `latest` (one image with all its tags). **Only the owner initiates a merge to `main` or a tag**; never do either unprompted.
- **Worktrees and PRs to `develop`:** do feature and multi-commit work on a short-lived branch in its own git worktree so parallel sessions don't share a tree: `git worktree add .worktrees/<branch> -b <branch> develop` (branch names `P5-01-short-name`; `.worktrees/` is ignored by git). Run `make install` there once (the venv and `node_modules` aren't shared; create the venv with Python 3.14 first, e.g. `python3.14 -m venv backend/.venv`, since a plain `python3` may be older). When the task is done and `make check` passes, push the branch, open a PR against `develop` (`gh pr create --base develop --head <branch>`), wait for CI, merge it yourself (`gh pr merge --merge --delete-branch`; no approval needed for `develop`), then `git worktree remove .worktrees/<branch>` and `git pull` on `develop`. Small single-commit changes (docs, the plan, a one-line fix) may still be committed straight to `develop`.
- **Releases** (only when the owner asks): `main` is protected (pull request required, CI checks `backend`/`frontend`/`e2e`/`secret-scan` must pass, no direct or force pushes, applies to admins). **Version numbers:** a release is the next patch (1.2.1 → 1.2.2) unless the owner asks for a minor or major one; never choose a minor or major bump yourself. `develop` builds already show the next patch (`scripts/build-info.sh`: `1.2.2-dev.N` after 1.2.1), so nothing is bumped on `develop` after a release. The release commit on `develop` bumps the version, dates the CHANGELOG section and runs `make unraid-template` (the Community Applications listing's update date and changelog; CI fails otherwise). Then `gh pr create --base main --head develop --title "Release X.Y.Z"`, wait for the checks, then `gh pr merge --merge` (a merge commit, never squash/rebase: `main` and `develop` must keep the same history). Tag `vX.Y.Z` on the merge commit if it's a version.
- Always verify (run `make check`) before committing. Commit per task, message `P5-01: short description`, then **always `git push`** (the feature branch, or `develop` for direct commits). Never commit half-done work. Stage files by name, never `git add -A`: other sessions may be editing the same tree.
- There is no access to the deployed server. All diagnostics come from logs read through the API (`/api/v1/logs`), so new code must log enough (redacted) to be debugged that way.
- Never commit, log or print secrets/tokens. Never put a complete token-shaped string in source (GitHub push protection blocks it); assemble canaries at runtime as `tests/test_redaction.py` does.
- PyPI package for python-plexapi is **`PlexAPI`** (import `plexapi`).
- All Plex access goes through `PlexGateway`; write the complete library set, never a partial one.
- Stop and ask at the human gates listed in the plan (real Plex server, Discord bot, IdP, Unraid).
