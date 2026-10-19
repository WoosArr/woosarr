#!/bin/sh
# Smoke-test a built image (plan P2-01/P2-02): health endpoint, Docker health
# status, /config persistence and graceful shutdown. Used by CI and `make smoke`.
set -eu

IMAGE="${1:-woosarr:dev}"
PORT="${2:-31337}"
NAME="woosarr-smoke-$$"
CONF="$(mktemp -d)"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    docker logs "$NAME" 2>&1 | tail -50 >&2 || true
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

echo "smoke test passed"
