#!/bin/sh
# Install SCOPED passwordless sudo for the relay lifecycle. Run as root ONCE on the
# appliance:   sudo sh /volume1/docker/mage-hands/synology-hands/scripts/install-sudo.sh
#
# Security model: only the up/down lifecycle is made passwordless, and only via ROOT-OWNED
# COPIES in /usr/local/sbin (which the relay user cannot edit). All other sudo — including
# anything genuinely destructive — still requires the password, so it stays gated behind a
# human. (The relay container itself is privileged by design; this only scopes *direct* sudo.)
set -e

BASE="${MAGE_HANDS_DIR:-/volume1/docker/mage-hands}/synology-hands"
RELAY_USER="${RELAY_USER:-magehands}"

# 1. Root-owned, relay-user-immutable copies of the lifecycle scripts.
#    These live under /usr/local (root-owned parent) so the relay user can neither edit the
#    copies nor swap out the directory — required for NOPASSWD to be safe.
mkdir -p /usr/local/sbin
install -m 0755 -o root -g root "$BASE/scripts/relay-up.sh"   /usr/local/sbin/mage-hands-relay-up
install -m 0755 -o root -g root "$BASE/scripts/relay-down.sh" /usr/local/sbin/mage-hands-relay-down

# 2. Ensure /etc/sudoers actually includes the drop-in dir.
if ! grep -Eq '^[#@]includedir[[:space:]]+/etc/sudoers.d' /etc/sudoers; then
    echo "@includedir /etc/sudoers.d" >> /etc/sudoers
    echo "added '@includedir /etc/sudoers.d' to /etc/sudoers"
fi

# 3. The scoped grant.
cat > /etc/sudoers.d/mage-hands <<EOF
# mage-hands: scoped passwordless sudo for the relay lifecycle only.
# $RELAY_USER may start/stop the relay without a password; everything else still
# requires the password (truly destructive ops are intentionally NOT covered).
$RELAY_USER ALL=(root) NOPASSWD: /usr/local/sbin/mage-hands-relay-up, /usr/local/sbin/mage-hands-relay-down
EOF
chmod 0440 /etc/sudoers.d/mage-hands

# 4. Validate before trusting it.
if command -v visudo >/dev/null 2>&1; then
    visudo -cf /etc/sudoers.d/mage-hands
fi
echo "installed: /usr/local/sbin/mage-hands-relay-{up,down} + /etc/sudoers.d/mage-hands"
echo "re-run this script after changing relay-up.sh / relay-down.sh to refresh the copies."
