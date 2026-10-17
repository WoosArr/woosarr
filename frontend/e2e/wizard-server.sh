#!/bin/sh
# Backend for e2e/wizards.spec.ts: real app with fake plex.tv/Discord (backend/tests/e2e_wizard_server.py).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$here/../../backend/.venv/bin/python}"
CONF="$(mktemp -d)"
cd "$here/../../backend"
export WOOSARR_CONFIG_DIR="$CONF" WOOSARR_STATIC_DIR="$here/../dist" WOOSARR_SETUP_CODE_REQUIRED=false \
    WOOSARR_PORT="${E2E_WIZARD_PORT:-31340}" WOOSARR_LOG_FORMAT=text
exec "$PY" -m tests.e2e_wizard_server
