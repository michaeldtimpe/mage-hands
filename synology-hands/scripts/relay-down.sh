#!/bin/sh
# Tear down Tailscale Serve and stop the relay. After this the attack surface is zero.
# Run on the NAS over SSH (or invoked by idle-watchdog.sh).
BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/synology-hands"

DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/local/bin/docker
TS="${TS_BIN:-$(command -v tailscale 2>/dev/null || true)}"
[ -n "$TS" ] || TS=/var/packages/Tailscale/target/bin/tailscale

sudo "$TS" serve --https=443 off 2>/dev/null || sudo "$TS" serve reset
sudo "$DOCKER" compose -f "$BASE/compose.yaml" down
echo "synology-hands is down."
