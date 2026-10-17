#!/bin/sh
# Unraid-friendly entrypoint: honor PUID/PGID (defaults 99:100 = nobody:users),
# make sure /config is writable, then drop privileges.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$(id -u)" = "0" ]; then
    # Re-map the runtime user/group to the requested ids.
    groupmod -o -g "$PGID" woosarr 2>/dev/null || true
    usermod -o -u "$PUID" -g "$PGID" woosarr 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec gosu woosarr "$@"
fi

exec "$@"
