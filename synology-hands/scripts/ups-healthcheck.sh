#!/bin/sh
# UPS *service health* check for a Synology that hosts a USB UPS, with a once-a-day down-alert.
#
# Why this exists: NUT's upsmon already alerts on power EVENTS (on-battery / low-battery / lost-
# comms) — but ONLY while it is running. It cannot tell you when UPS monitoring itself is DOWN:
# daemon stopped, USB not enumerating, wrong driver (all of which we hit on alpha 2026-05, silently).
# This check closes that gap. It is read-only — it never changes UPS state.
#
# Run as ROOT; schedule DAILY via DSM Task Scheduler and tick "send run details by email" /
# "notify when the script terminates abnormally" — a DOWN result exits non-zero, so DSM emails you
# (once per day). Visibility on a DOWN result is layered: a dedicated logfile (every run), a
# daemon.err syslog line (-> /var/log/messages + Log Center), and a DSM desktop notification.
#
#   ups-healthcheck.sh           # log health; on DOWN: notify + exit 1
#   ups-healthcheck.sh --quiet   # suppress stdout when healthy (only speaks up when DOWN)
#
# Logfile: $UPS_HEALTH_LOG (default /var/log/mage-ups-health.log).
# Exit: 0 = healthy (or no UPS configured here), 1 = configured UPS is DOWN.
set -u
export PATH="/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$PATH"

TAG=mage-ups-healthcheck
LOGFILE="${UPS_HEALTH_LOG:-/var/log/mage-ups-health.log}"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

ts() { date '+%Y-%m-%dT%H:%M:%S'; }
logfile() { printf '%s %s %s\n' "$(ts)" "$1" "$2" >> "$LOGFILE" 2>/dev/null || true; }

healthy() { logfile OK "$1"; [ "$QUIET" = 1 ] || echo "$(ts) $TAG: OK: $1"; exit 0; }
down() {  # logfile + Log Center (daemon.err) + DSM desktop notif + non-zero exit (=> task email)
    logfile DOWN "$1"
    logger -p daemon.err -t "$TAG" "$1" 2>/dev/null || true   # err/warning land in /var/log/messages; notice/info don't
    echo "$(ts) $TAG: DOWN: $1"
    synodsmnotify @administrators "UPS monitoring DOWN ($(hostname))" "$1" >/dev/null 2>&1 || true
    exit 1
}

# Only act where a UPS is actually expected, so this is safe to deploy fleet-wide.
EXPECTED=$(synogetkeyvalue /usr/syno/etc/ups/synoups.conf ups_enabled 2>/dev/null || true)
[ "$EXPECTED" = "yes" ] || healthy "no UPS configured on this host (ups_enabled='${EXPECTED:-unset}'); skipping"

# upsd answering + a readable status is the authoritative "monitoring is alive" signal.
NAMES=$(upsc -l 2>/dev/null || true)
[ -n "$NAMES" ] || down "ups_enabled=yes but upsd is not answering (no UPS instances) — UPS service is stopped or the UPS isn't connected. Check 'synosystemctl get-active-status ups-usb' and the USB link."

NAME=$(echo "$NAMES" | head -1)
STATUS=$(upsc "$NAME" ups.status 2>/dev/null || true)
MODEL=$(upsc "$NAME" ups.model 2>/dev/null || true)
[ -n "$STATUS" ] || down "UPS [$NAME] is registered but reporting no status — the driver isn't reading the unit (USB enumeration / driver). Check the USB cable/port."

# OL=online, OB=on battery (a power event upsmon handles) — either way, monitoring is WORKING.
healthy "UPS [$NAME] reporting (status='$STATUS' model='${MODEL:-?}')"
