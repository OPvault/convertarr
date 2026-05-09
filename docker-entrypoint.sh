#!/bin/sh
# Convertarr container entrypoint.
#
# Runs as root just long enough to:
#   * align the in-image `convertarr` account with the user's PUID/PGID
#   * make /config writable (the bind-mount is often root-owned)
# Then drops privileges via gosu and exec's the actual app.
#
# Honours:
#   PUID / PGID  — uid/gid the app should run as. Default 1000:1000. Set 0:0
#                  to keep running as root (rare; only useful when the bind-
#                  mount source is owned by root and you can't chown it).
#   TZ           — standard tzdata-style zone string. Python honours it
#                  automatically when tzdata is installed (it is).
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# If we're not root we can't chown or change uid; trust the caller. This
# matters when someone runs `docker run --user 1234 ...` deliberately.
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

mkdir -p /config

if [ "$PUID" = "0" ] && [ "$PGID" = "0" ]; then
    # User explicitly asked to run as root. Make sure /config exists and
    # then exec — no privilege drop, no usermod.
    chown -R 0:0 /config 2>/dev/null || true
    exec "$@"
fi

# Reshape the bundled convertarr account to match PUID/PGID so files the
# app creates land with sane ownership on the host bind-mount. -o lets us
# reuse an existing uid/gid (e.g. when PUID=1000 happens to collide with
# another system user from a base-image change down the line).
existing_uid="$(id -u convertarr)"
existing_gid="$(id -g convertarr)"
if [ "$existing_gid" != "$PGID" ]; then
    groupmod -o -g "$PGID" convertarr
fi
if [ "$existing_uid" != "$PUID" ]; then
    usermod -o -u "$PUID" convertarr
fi

# Best-effort recursive chown. If the bind-mount is huge (e.g. someone
# bind-mounted media in here by mistake) this could be slow — that's a
# misconfig the user should fix, not something we should silently absorb.
chown -R "$PUID:$PGID" /config 2>/dev/null || true

exec gosu convertarr "$@"
