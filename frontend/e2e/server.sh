#!/bin/sh
# Backend for Playwright: demo data (fake Plex + Discord), fresh /config, no
# setup code, serving the built UI. Never touches anything real.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$here/../../backend/.venv/bin/python}"
CONF="$(mktemp -d)"
export WOOSARR_CONFIG_DIR="$CONF" WOOSARR_STATIC_DIR="$here/../dist" WOOSARR_DEMO=true \
    WOOSARR_SETUP_CODE_REQUIRED=false WOOSARR_PORT="${E2E_PORT:-31338}" WOOSARR_LOG_FORMAT=text
exec "$PY" -m woosarr
