#!/bin/sh
# Auto-update Plex Media Server on a Synology, bypassing Package Center (which lags Plex's own
# releases badly — the same problem that left Tailscale 2 years behind; see tailscale-update.sh).
# Run as ROOT; schedule weekly via DSM Task Scheduler (do NOT hand-edit /etc/crontab — DSM owns it).
#
# It reads Plex's own downloads catalog, picks the build matching THIS box's DSM major + CPU arch,
# compares against the installed version, and (if newer) downloads + sha1-verifies + installs the
# .spk, then starts Plex. Safe no-op on hosts without Plex.
#
#   plex-update.sh            # check, and install if a newer build exists
#   plex-update.sh --check    # report only; never installs. exit 0 = up to date, 10 = update available
#
# Channel: stable/public by default. Set PLEX_TOKEN=<your Plex token> to track the Plex Pass channel.
# Temp dir for the ~70 MB .spk: $PLEX_UPDATE_TMP (default /tmp).
set -eu

# syno* tools live outside a cron/nsenter login PATH (the recurring lesson); put them back.
export PATH="/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$PATH"

PKG=PlexMediaServer
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') plex-update: $*"; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

for bin in jq synopkg curl sha1sum; do
    command -v "$bin" >/dev/null 2>&1 || { log "ERROR: required tool '$bin' not found"; exit 1; }
done

# No-op where Plex isn't installed (so this is safe to roll out fleet-wide).
INSTALLED=$(synopkg version "$PKG" 2>/dev/null || true)
[ -n "$INSTALLED" ] || { log "$PKG not installed on this host; nothing to do."; exit 0; }

# ver_ge A B  ->  true if A >= B (version sort)
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# Plex splits its Synology catalog by DSM major; pick the matching key.
DSM_VER=$(grep '^productversion=' /etc/VERSION | cut -d'"' -f2)
if   ver_ge "$DSM_VER" 7.2.2; then NAS_KEY="Synology (DSM 7.2.2+)"
elif ver_ge "$DSM_VER" 7.0;   then NAS_KEY="Synology (DSM 7)"
elif ver_ge "$DSM_VER" 6.0;   then NAS_KEY="Synology (DSM 6)"
else log "ERROR: unsupported DSM version '$DSM_VER'"; exit 1; fi

case "$(uname -m)" in
    x86_64)      BUILD=linux-x86_64 ;;
    i686|i386)   BUILD=linux-x86 ;;
    aarch64|arm64) BUILD=linux-aarch64 ;;
    *) log "ERROR: unmapped CPU arch '$(uname -m)' — extend the BUILD case (note: Plex has two ARMv7 builds by model series)"; exit 1 ;;
esac

# Fetch Plex's catalog (stable, or the Plex Pass channel if a token is provided).
API="https://plex.tv/api/downloads/5.json"
if [ -n "${PLEX_TOKEN:-}" ]; then
    J=$(curl -fsSL --max-time 30 -H "X-Plex-Token: $PLEX_TOKEN" "$API?channel=plexpass") \
        || { log "ERROR: failed to fetch Plex catalog (plexpass)"; exit 1; }
else
    J=$(curl -fsSL --max-time 30 "$API") || { log "ERROR: failed to fetch Plex catalog"; exit 1; }
fi

LATEST=$(printf '%s' "$J" | jq -r --arg k "$NAS_KEY" '.nas[$k].version // empty')
SPK_URL=$(printf '%s' "$J" | jq -r --arg k "$NAS_KEY" --arg b "$BUILD" '.nas[$k].releases[]? | select(.build==$b) | .url')
SPK_SUM=$(printf '%s' "$J" | jq -r --arg k "$NAS_KEY" --arg b "$BUILD" '.nas[$k].releases[]? | select(.build==$b) | .checksum')
[ -n "$LATEST" ] && [ -n "$SPK_URL" ] && [ -n "$SPK_SUM" ] \
    || { log "ERROR: could not resolve a build for [$NAS_KEY / $BUILD] in the catalog"; exit 1; }

IV=${INSTALLED%%-*}   # strip Synology/git build suffix, leaving the dotted version
LV=${LATEST%%-*}
log "DSM $DSM_VER  [$NAS_KEY / $BUILD]  installed=$INSTALLED  latest=$LATEST"

if [ "$IV" = "$LV" ]; then log "already up to date ($IV)."; exit 0; fi
if [ "$(printf '%s\n%s\n' "$IV" "$LV" | sort -V | tail -1)" != "$LV" ]; then
    log "installed ($IV) is newer than catalog ($LV); not downgrading."; exit 0
fi

log "update available: $IV -> $LV"
[ "$CHECK_ONLY" = 1 ] && exit 10

TMP=$(mktemp -d "${PLEX_UPDATE_TMP:-/tmp}/plex-update.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
SPK="$TMP/plex.spk"
log "downloading $SPK_URL"
curl -fsSL --max-time 300 -o "$SPK" "$SPK_URL" || { log "ERROR: download failed"; exit 1; }
GOT=$(sha1sum "$SPK" | cut -d' ' -f1)
[ "$GOT" = "$SPK_SUM" ] || { log "ERROR: sha1 mismatch (got $GOT, want $SPK_SUM)"; exit 1; }
log "sha1 verified ($GOT); installing (Plex will restart)..."
synopkg install "$SPK" || { log "ERROR: synopkg install failed"; exit 1; }
synopkg start "$PKG" >/dev/null 2>&1 || true
sleep 3
NOW=$(synopkg version "$PKG" 2>/dev/null || echo unknown)
if [ "${NOW%%-*}" = "$LV" ]; then log "update complete: now $NOW"; else log "WARNING: post-install version is $NOW (expected $LV)"; fi
