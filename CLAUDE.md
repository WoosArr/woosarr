# WooSarr — instructions for Claude Code

WooSarr syncs Plex library access from Discord roles. Single Docker container (Unraid), web UI on port 2000.

**Read `docs/action-plan.md` first.** It holds the status snapshot, decisions, non-negotiables, pitfalls and the task list. Work the next unblocked task, run its *Verify* command, update the plan in the same commit.

## Commands
- Backend: `cd backend && pip install -e ".[dev]" && ruff check . && python -m pytest -q`
- Frontend: `cd frontend && npm install && npm run build`
- Image: `docker build -f docker/Dockerfile -t woosarr:dev .`
- CI: `gh run list --workflow "Docker publish" --limit 3`

## Rules that matter
- Commit per task, message `P5-01: short description`. **Do not `git push` unless the user asks**: pushes to `main` publish a Docker image.
- Never commit, log or print secrets/tokens. Never put a complete token-shaped string in source (GitHub push protection blocks it); assemble canaries at runtime as `tests/test_redaction.py` does.
- PyPI package for python-plexapi is **`PlexAPI`** (import `plexapi`).
- All Plex access goes through `PlexGateway`; write the complete library set, never a partial one.
- Stop and ask at the human gates listed in the plan (real Plex server, Discord bot, IdP, Unraid).
