#!/bin/sh
# Quick router-monitor summary for today (or $1 = YYYY-MM-DD). Reads the per-day health JSONL and
# prints the latest snapshot + a count of edge events. No jq dependency (busybox grep/sed/awk).
DAY="${1:-$(date -u +%F)}"
DIR="${LOG_DIR:-/data}"
H="$DIR/health-$DAY.jsonl"
E="$DIR/events-$DAY.jsonl"
S="$DIR/syslog-$DAY.log"

[ -f "$H" ] || { echo "no health log for $DAY ($H)"; exit 1; }

echo "== router-monitor $DAY =="
samples=$(grep -c . "$H")
unreach=$(grep -c '"reachable":false' "$H")
echo "health samples: $samples   (unreachable: $unreach)"

last=$(grep '"reachable":true' "$H" | tail -1)
if [ -n "$last" ]; then
  get() { printf '%s' "$last" | sed -n "s/.*\"$1\":\\([^,}]*\\).*/\\1/p"; }
  echo "latest: uptime_s=$(get uptime_s) reboot=$(get reboot) load1=$(get load1) mem_used_pct=$(get mem_used_pct)"
  echo "        conntrack=$(get conntrack)/$(get conntrack_max) temp_c=$(get temp_c) dhcp=$(get dhcp_leases) arp=$(get arp_reachable)"
  echo "        wan_up=$(get wan_up) wan_ip=$(get wan_ip) fw=$(get fw) fw_update_pending=$(get fw_update_pending)"
fi

if [ -f "$E" ]; then
  echo "events today: $(grep -c . "$E")"
  for t in reboot wan_ip_change firmware_state_change router_unreachable router_reachable; do
    n=$(grep -c "\"event\":\"$t\"" "$E" 2>/dev/null)
    [ "${n:-0}" -gt 0 ] && echo "  $t: $n"
  done
fi
[ -f "$S" ] && echo "syslog mirrored: $(grep -c . "$S") lines ($(du -h "$S" | cut -f1))"
