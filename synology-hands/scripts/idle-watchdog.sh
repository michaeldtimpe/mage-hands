#!/bin/sh
# Auto-stop the relay after IDLE_SECONDS of inactivity. Install as a DSM Task Scheduler
# root job running every 5 minutes. Idle window defaults to 30 minutes.
#
# The relay touches <logs>/last_activity on every tool call; this compares that timestamp
# to now and tears the relay down when it goes stale.
IDLE_SECONDS="${IDLE_SECONDS:-1800}"
BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/synology-hands"
LAST_ACTIVITY="$BASE/logs/last_activity"

DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/local/bin/docker

# Only act if the relay is actually running.
sudo "$DOCKER" inspect -f '{{.State.Running}}' synology-hands 2>/dev/null | grep -q true || exit 0
[ -f "$LAST_ACTIVITY" ] || exit 0

last=$(cut -d. -f1 "$LAST_ACTIVITY" 2>/dev/null)
now=$(date +%s)
[ -n "$last" ] || exit 0

if [ $((now - last)) -ge "$IDLE_SECONDS" ]; then
    echo "idle for $((now - last))s (>= ${IDLE_SECONDS}s); stopping relay"
    "$BASE/scripts/relay-down.sh"
fi
