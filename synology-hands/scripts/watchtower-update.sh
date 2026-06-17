#!/bin/sh
# mage-hands: monthly one-shot container auto-update via Watchtower.
#
# Scheduled by DSM Task Scheduler as a WEEKLY task (Tuesday 12:00); this script gates so it only
# acts on the FIRST Tuesday of the month, then runs Watchtower ONCE and exits. `docker run --rm`
# removes the container afterward, so nothing keeps running between monthly passes.
#
# Scope: Watchtower runs with WATCHTOWER_LABEL_ENABLE unset (=false), so EVERY running container
# with a registry image is a candidate (shelfmark, calibre-web-automated, the *arr stack,
# audiobookshelf, ...). Locally-built images with no registry (the mage-hands relay) are skipped
# automatically. WATCHTOWER_CLEANUP prunes the superseded images. Run as ROOT.
#
#   watchtower-update.sh                  # gate to first Tuesday, then update + cleanup, then exit
#   FORCE=1 watchtower-update.sh          # ignore the first-Tuesday gate (manual run)
#   MONITOR_ONLY=1 watchtower-update.sh   # report what WOULD update; recreate nothing
set -eu
export PATH="/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$PATH"
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') watchtower-update: $*"; }

# First-Tuesday guard: the first occurrence of any weekday always lands on day-of-month 1..7.
DOM=$(date +%d | sed 's/^0*//')
if [ "${FORCE:-0}" != 1 ] && [ "${DOM:-99}" -gt 7 ]; then
    log "today is day $DOM of the month, not the first Tuesday; nothing to do."
    exit 0
fi

IMAGE="${WATCHTOWER_IMAGE:-nickfedor/watchtower:latest}"
EXTRA=""
[ "${MONITOR_ONLY:-0}" = 1 ] && EXTRA="--monitor-only"

log "starting one-shot Watchtower ($IMAGE) ${EXTRA:+[$EXTRA]} ..."
docker pull "$IMAGE" >/dev/null 2>&1 || log "WARN: pull failed; using cached image if present"
docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e TZ=America/Chicago \
    -e WATCHTOWER_CLEANUP=true \
    -e WATCHTOWER_INCLUDE_RESTARTING=true \
    -e DOCKER_API_VERSION=1.43 \
    "$IMAGE" --run-once $EXTRA
log "done; watchtower container has exited and been removed."
