#!/bin/sh
# Install SCOPED passwordless sudo for the router-hands lifecycle. Run as root ONCE on kappa:
#   sudo sh /volume1/docker/mage-hands/router-hands/scripts/install-sudo.sh
#
# Security model: only the up/down lifecycle is made passwordless, and only via ROOT-OWNED COPIES
# in /usr/local/sbin (which the relay user cannot edit). All other sudo still requires the
# password, so anything genuinely destructive stays gated behind a human.
#
# DISTINCT names (mage-hands-router-relay-{up,down} + /etc/sudoers.d/mage-hands-router) so this
# coexists with synology-hands' own grant on the same box.
set -e

BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/router-hands"
RELAY_USER="${RELAY_USER:-magehands}"

# 1. Root-owned, relay-user-immutable copies of the lifecycle scripts.
mkdir -p /usr/local/sbin
install -m 0755 -o root -g root "$BASE/scripts/relay-up.sh"   /usr/local/sbin/mage-hands-router-relay-up
install -m 0755 -o root -g root "$BASE/scripts/relay-down.sh" /usr/local/sbin/mage-hands-router-relay-down

# 2. Ensure /etc/sudoers actually includes the drop-in dir.
if ! grep -Eq '^[#@]includedir[[:space:]]+/etc/sudoers.d' /etc/sudoers; then
    echo "@includedir /etc/sudoers.d" >> /etc/sudoers
    echo "added '@includedir /etc/sudoers.d' to /etc/sudoers"
fi

# 3. The scoped grant (separate file from synology's /etc/sudoers.d/mage-hands).
cat > /etc/sudoers.d/mage-hands-router <<EOF
# mage-hands router-hands: scoped passwordless sudo for the relay lifecycle only.
# $RELAY_USER may start/stop the router relay without a password; everything else still
# requires the password (truly destructive ops are intentionally NOT covered).
$RELAY_USER ALL=(root) NOPASSWD: /usr/local/sbin/mage-hands-router-relay-up, /usr/local/sbin/mage-hands-router-relay-down
EOF
chmod 0440 /etc/sudoers.d/mage-hands-router

# 4. Validate before trusting it.
if command -v visudo >/dev/null 2>&1; then
    visudo -cf /etc/sudoers.d/mage-hands-router
fi
echo "installed: /usr/local/sbin/mage-hands-router-relay-{up,down} + /etc/sudoers.d/mage-hands-router"
echo "re-run this script after changing relay-up.sh / relay-down.sh to refresh the copies."
