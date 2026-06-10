#!/bin/sh
# Bring the synology-hands relay up, wait for health, then expose it over Tailscale Serve
# (HTTPS, tailnet-private). Run on the NAS over SSH.
set -e

BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/synology-hands"
COMPOSE="$BASE/compose.yaml"

# Synology's sudo secure_path doesn't include these; resolve full paths.
DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/local/bin/docker
TS="${TS_BIN:-$(command -v tailscale 2>/dev/null || true)}"
[ -n "$TS" ] || TS=/var/packages/Tailscale/target/bin/tailscale

sudo "$DOCKER" compose -f "$COMPOSE" up -d --build

# Wait for the container healthcheck before exposing it.
status=starting
i=0
while [ "$i" -lt 30 ]; do
    status=$(sudo "$DOCKER" inspect -f '{{.State.Health.Status}}' synology-hands 2>/dev/null || echo starting)
    [ "$status" = healthy ] && break
    i=$((i + 1))
    sleep 1
done
if [ "$status" != healthy ]; then
    echo "relay did not become healthy (status=$status)" >&2
    exit 1
fi

# Idempotent: re-asserting the same mapping is a no-op. NOTE: if you change PORT in .env
# (compose interpolates it), update this proxy target by hand to match.
sudo "$TS" serve --bg --https=443 http://localhost:8787
sudo "$TS" serve status
echo "synology-hands is up and served over Tailscale."
