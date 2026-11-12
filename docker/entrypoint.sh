#!/bin/sh
# Two ways to start, both tested by scripts/smoke-test.sh:
# - as root (Unraid, Docker, Compose): honor PUID/PGID (defaults 99:100 = nobody:users), make sure
#   /config is writable, then drop privileges for good;
# - as a non-root user already (TrueNAS apps run as 568, `docker run --user`): run WooSarr directly;
#   /config must belong to that user.
set -e

# New files (database, logs, backups) are not readable by other users on the host.
umask 027

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$PUID" = "0" ]; then
    echo "WARNING: PUID=0 runs WooSarr as root; use an unprivileged user (Unraid default 99)." >&2
fi

if [ "$(id -u)" = "0" ]; then
    # Re-map the runtime user/group to the requested ids.
    groupmod -o -g "$PGID" woosarr 2>/dev/null || true
    usermod -o -u "$PUID" -g "$PGID" woosarr 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec gosu woosarr "$@"
fi

exec "$@"
