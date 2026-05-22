#!/bin/sh
# Self-update Tailscale, bypassing Synology Package Center (which lags badly — it left these
# boxes stuck on 1.58.2 for ~2 years). Run as root; schedule weekly via DSM Task Scheduler.
#
# The PATH prefix is REQUIRED: `tailscale update` shells out to `synopkg`, and cron / nsenter
# environments don't carry DSM's login PATH, so a bare `synopkg` fails with exit 127.
export PATH="/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$PATH"
TS="${TS_BIN:-/var/packages/Tailscale/target/bin/tailscale}"

echo "$(date '+%Y-%m-%dT%H:%M:%S') tailscale-update: current $("$TS" version 2>/dev/null | head -1)"
exec "$TS" update --yes
