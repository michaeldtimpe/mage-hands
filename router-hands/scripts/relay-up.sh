#!/bin/sh
# Bring router-hands up: start the Tailscale sidecar + the relay, wait for the relay to be healthy,
# then verify SSH egress to the router. Ingress is DECLARATIVE (the sidecar's TS_SERVE_CONFIG) —
# there is NO `tailscale serve` CLI call here, unlike synology-hands. Run on kappa over SSH.
set -e

BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/router-hands"
COMPOSE="$BASE/compose.yaml"

# Synology's sudo secure_path doesn't include /usr/local/bin; resolve the full docker path.
DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/local/bin/docker

sudo "$DOCKER" compose -f "$COMPOSE" up -d --build

# Wait for the RELAY container healthcheck (the sidecar is gated by depends_on already).
status=starting
i=0
while [ "$i" -lt 30 ]; do
    status=$(sudo "$DOCKER" inspect -f '{{.State.Health.Status}}' router-hands 2>/dev/null || echo starting)
    [ "$status" = healthy ] && break
    i=$((i + 1))
    sleep 1
done
if [ "$status" != healthy ]; then
    echo "relay did not become healthy (status=$status)" >&2
    sudo "$DOCKER" logs --tail 30 router-hands 2>&1 || true
    exit 1
fi

# Best-effort: confirm the sidecar reached the tailnet.
sudo "$DOCKER" exec router-hands-ts tailscale status --peers=false 2>/dev/null | head -1 \
    || echo "note: could not read sidecar tailscale status" >&2

# Non-blocking SSH-egress check: prove the relay can actually reach the router over the LAN (the
# userspace-netns egress risk — see lessons.md). Prints PASS/FAIL; does NOT fail the bring-up.
if sudo "$DOCKER" exec router-hands sh -c '
    ssh -o BatchMode=yes -o ConnectTimeout="${ROUTER_CONNECT_TIMEOUT:-10}" \
        -o StrictHostKeyChecking="${ROUTER_STRICT_HOST_KEY:-yes}" \
        -o UserKnownHostsFile="${ROUTER_KNOWN_HOSTS:-/secrets/known_hosts}" \
        -i "${ROUTER_SSH_KEY:-/secrets/router_key}" -p "${ROUTER_PORT:-22}" \
        "${ROUTER_USER:-admin}@${ROUTER_HOST}" true' 2>/dev/null; then
    echo "SSH egress to router: PASS"
else
    echo "SSH egress to router: FAIL — check ROUTER_HOST / key / known_hosts and the userspace-netns" >&2
    echo "  LAN-egress fallback (TS_USERSPACE=false) in lessons.md; relay is up but tools error until this passes." >&2
fi

echo "router-hands is up; served at https://router1.<tailnet>.ts.net/mcp via the sidecar."
