#!/bin/sh
# Smoke-test a built image (plan P2-01/P2-02): health endpoint, Docker health
# status, /config persistence and graceful shutdown. Used by CI and `make smoke`.
set -eu

IMAGE="${1:-woosarr:dev}"
PORT="${2:-31337}"
NAME="woosarr-smoke-$$"
NAME568="woosarr-smoke-568-$$"  # the non-root (TrueNAS-style) run at the end
CONF="$(mktemp -d)"
CURRENT="$NAME"  # whose log fail() shows

cleanup() {
    docker rm -f "$NAME" "$NAME568" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    docker logs "$CURRENT" 2>&1 | tail -50 >&2 || true
    exit 1
}

# Same hardening the Unraid template sets (ExtraParams): no privilege re-escalation after gosu.
docker run -d --name "$NAME" -p "$PORT:31337" -v "$CONF:/config" --security-opt no-new-privileges:true \
    --health-interval=2s --health-start-period=5s "$IMAGE" >/dev/null

echo "waiting for /api/health on :$PORT"
i=0
until curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -le 30 ] || fail "health endpoint never answered"; sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/api/health"; echo
code=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/v1/logs?limit=5")
[ "$code" = "401" ] || fail "logs API should require auth, got HTTP $code"
curl -fsS "http://127.0.0.1:$PORT/api/v1/auth/status" | grep -q '"setup_required":true' || fail "auth status"
docker logs "$NAME" 2>&1 | grep -q "first-run setup code" || fail "setup code not printed to container log"
curl -fsS "http://127.0.0.1:$PORT/" | grep -qi "<div id=\"root\">" || fail "UI not served"
# pip is removed from the image (build tool only; its bundled packages were flagged by Trivy).
docker exec "$NAME" python -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('pip') is not None)" \
    || fail "pip is still in the image"

echo "waiting for docker health status"
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = "healthy" ]; do
    i=$((i + 1)); [ "$i" -le 30 ] || fail "container never became healthy"; sleep 1
done

start=$(date +%s)
docker stop "$NAME" >/dev/null
took=$(( $(date +%s) - start ))
echo "docker stop took ${took}s"
[ "$took" -lt 10 ] || fail "shutdown took ${took}s (>= 10s means SIGKILL)"
code="$(docker inspect -f '{{.State.ExitCode}}' "$NAME")"
[ "$code" != "137" ] || fail "container was SIGKILLed"

# The log file must have been written to the mounted volume (owned by PUID).
sudo_cmd=""; [ "$(id -u)" = "0" ] || ! command -v sudo >/dev/null || sudo_cmd="sudo"
LOG="$CONF/logs/woosarr.log"
$sudo_cmd test -s "$LOG" || fail "/config/logs/woosarr.log not persisted"

# Restart on the same volume: log file is appended, not replaced.
# Read through sudo: the file belongs to PUID, and a shell redirect would not.
before=$($sudo_cmd cat "$LOG" | wc -l)
docker start "$NAME" >/dev/null
i=0
until curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -le 30 ] || fail "health endpoint never answered after restart"; sleep 1
done
docker stop "$NAME" >/dev/null
after=$($sudo_cmd cat "$LOG" | wc -l)
[ "$after" -gt "$before" ] || fail "log file not appended across restart"

# TrueNAS-style start: as a fixed non-root user (568, TrueNAS's "apps"), never root, no capabilities.
# The entrypoint then skips PUID/PGID and the chown and runs WooSarr directly; /config must already
# belong to that user (TrueNAS datasets and ixVolumes do).
CONF2="$(mktemp -d)"
$sudo_cmd chown 568:568 "$CONF2"
docker run -d --name "$NAME568" --user 568:568 -p "$PORT:31337" -v "$CONF2:/config" \
    --security-opt no-new-privileges:true --cap-drop ALL \
    --health-interval=2s --health-start-period=5s "$IMAGE" >/dev/null
CURRENT="$NAME568"
i=0
until curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -le 30 ] || fail "non-root start: health endpoint never answered"; sleep 1
done
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME568")" = "healthy" ]; do
    i=$((i + 1)); [ "$i" -le 30 ] || fail "non-root start: container never became healthy"; sleep 1
done
[ "$(docker exec "$NAME568" id -u)" = "568" ] || fail "non-root start: not running as 568"
$sudo_cmd test -s "$CONF2/app.db" || fail "non-root start: database not created in /config"
owner=$($sudo_cmd stat -c %u "$CONF2/app.db")
[ "$owner" = "568" ] || fail "non-root start: app.db owned by $owner, not 568"
docker stop "$NAME568" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$NAME568")" != "137" ] || fail "non-root start: SIGKILLed on stop"
echo "non-root (568) start passed"

echo "smoke test passed"
