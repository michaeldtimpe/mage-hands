#!/bin/sh
# Internet connectivity health logger — runs in a tiny always-on container on kappa.
# Appends ONE JSON line per interval to $LOG. Pure busybox (alpine), no extra packages.
#
# Each sample distinguishes three layers so an outage is diagnosable, not just "internet down":
#   gw_*     LAN -> router (192.168.1.1): if this fails, the problem is kappa<->router, not the ISP
#   wan_*    router -> internet (pings 1.1.1.1 / 8.8.8.8): the actual WAN/ISP health signal
#   dns_ok   name resolution works (a common "internet is up but nothing loads" failure mode)
set -u

LOG="${LOG:-/data/connectivity.jsonl}"
INTERVAL="${INTERVAL:-60}"                  # seconds between samples
GATEWAY="${GATEWAY:-192.168.1.1}"           # LAN gateway (the router)
TARGETS="${TARGETS:-1.1.1.1 8.8.8.8}"       # WAN ping targets (space-separated)
DNS_NAME="${DNS_NAME:-google.com}"          # name to resolve for the DNS health check
PING_COUNT="${PING_COUNT:-3}"
MAX_LINES="${MAX_LINES:-525600}"            # ~1 year @ 1/min; older lines trimmed beyond this

mkdir -p "$(dirname "$LOG")"

# probe HOST -> echoes "rtt_avg_ms loss_pct"  (rtt empty when 100% loss / unreachable)
probe() {
  out=$(ping -c "$PING_COUNT" -W 2 -q "$1" 2>/dev/null)
  loss=$(echo "$out" | sed -n 's/.* \([0-9]*\)% packet loss.*/\1/p')
  rtt=$(echo "$out"  | sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p')
  [ -z "$loss" ] && loss=100
  echo "$rtt $loss"
}

echo "net-monitor: logging to $LOG every ${INTERVAL}s (gw=$GATEWAY targets='$TARGETS')"
while :; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  set -- $(probe "$GATEWAY"); gw_rtt=$1; gw_loss=$2
  if [ "$gw_loss" -lt 100 ] 2>/dev/null; then gw_up=true; else gw_up=false; fi

  wan_up=false; targets_json=""
  for t in $TARGETS; do
    set -- $(probe "$t"); rtt=$1; loss=$2
    [ "$loss" -lt 100 ] 2>/dev/null && wan_up=true
    targets_json="${targets_json:+$targets_json,}{\"target\":\"$t\",\"rtt_ms\":${rtt:-null},\"loss_pct\":$loss}"
  done

  if nslookup "$DNS_NAME" >/dev/null 2>&1; then dns_ok=true; else dns_ok=false; fi

  printf '{"ts":"%s","gw_up":%s,"gw_rtt_ms":%s,"gw_loss_pct":%s,"wan_up":%s,"dns_ok":%s,"targets":[%s]}\n' \
    "$ts" "$gw_up" "${gw_rtt:-null}" "$gw_loss" "$wan_up" "$dns_ok" "$targets_json" >> "$LOG"

  lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  if [ "$lines" -gt "$MAX_LINES" ]; then
    tail -n "$MAX_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi

  sleep "$INTERVAL"
done
