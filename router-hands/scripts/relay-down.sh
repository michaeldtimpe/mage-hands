#!/bin/sh
# Tear down router-hands: stop the relay AND the Tailscale sidecar. Serve and the tailnet node go
# away with the sidecar, so the attack surface returns to zero. Run on kappa over SSH (or invoked
# by idle-watchdog.sh).
BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/router-hands"

DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/local/bin/docker

sudo "$DOCKER" compose -f "$BASE/compose.yaml" down
echo "router-hands is down."
