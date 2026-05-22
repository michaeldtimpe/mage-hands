#!/bin/sh
# Quick connectivity summary from the JSONL log — pure shell, no jq required.
# Usage: summary.sh [logfile]   (default /data/connectivity.jsonl)
LOG="${1:-/data/connectivity.jsonl}"
[ -f "$LOG" ] || { echo "no log at $LOG"; exit 1; }

# `tr -dc 0-9` strips busybox wc's space padding (and any stray output) so $(()) stays numeric.
n() { tr -dc '0-9'; }
total=$(wc -l < "$LOG" | n);            total=${total:-0}
wan_down=$(grep -c '"wan_up":false' "$LOG" | n); wan_down=${wan_down:-0}
dns_fail=$(grep -c '"dns_ok":false' "$LOG" | n); dns_fail=${dns_fail:-0}
lan_down=$(grep -c '"gw_up":false'  "$LOG" | n); lan_down=${lan_down:-0}
first=$(head -1 "$LOG" | sed -n 's/.*"ts":"\([^"]*\)".*/\1/p')
last=$(tail -1 "$LOG"  | sed -n 's/.*"ts":"\([^"]*\)".*/\1/p')

up=$((total - wan_down))
pct="n/a"
[ "$total" -gt 0 ] && pct="$((up * 100 / total))%"

echo "window:        $first  ->  $last"
echo "samples:       $total"
echo "WAN reachable: $up / $total  ($pct)"
echo "WAN down:      $wan_down"
echo "DNS failures:  $dns_fail"
echo "LAN/gw down:   $lan_down"
echo
echo "(latency percentiles need jq:  jq -s 'map(.targets[0].rtt_ms)|sort' $LOG )"
