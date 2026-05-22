#!/bin/sh
# Quick connectivity summary from the per-day JSONL logs — pure busybox, no jq/bc required.
# Usage: summary.sh [dir|file]   (default: /data, i.e. all connectivity-*.jsonl)
ARG="${1:-/data}"
if [ -d "$ARG" ]; then set -- "$ARG"/connectivity*.jsonl; else set -- "$ARG"; fi
[ -e "$1" ] || { echo "no logs at ${ARG}"; exit 1; }
nfiles=$#

# `tr -dc 0-9` strips busybox wc padding / grep -c's exit-1-on-zero so $(()) stays numeric.
n() { tr -dc '0-9'; }
total=$(cat "$@" | wc -l | n);                          total=${total:-0}
wan_down=$(cat "$@" | grep -c '"wan_up":false' | n);    wan_down=${wan_down:-0}
dns_fail=$(cat "$@" | grep -c '"dns_ok":false' | n);    dns_fail=${dns_fail:-0}
lan_down=$(cat "$@" | grep -c '"gw_up":false'  | n);    lan_down=${lan_down:-0}
v6_down=$(cat "$@"  | grep -c '"ipv6_ok":false' | n);   v6_down=${v6_down:-0}
first=$(cat "$@" | head -1 | sed -n 's/.*"ts":"\([^"]*\)".*/\1/p')
last=$(cat "$@"  | tail -1 | sed -n 's/.*"ts":"\([^"]*\)".*/\1/p')
tput=$(grep -h '"tput"' "$@" 2>/dev/null | tail -1 | sed -n 's/.*"tput":{\([^}]*\)}.*/\1/p')

up=$((total - wan_down))
pct="n/a"; [ "$total" -gt 0 ] && pct="$((up * 100 / total))%"

echo "files:         $nfiles"
echo "window:        $first  ->  $last"
echo "samples:       $total"
echo "WAN reachable: $up / $total  ($pct)"
echo "WAN down:      $wan_down"
echo "DNS failures:  $dns_fail"
echo "LAN/gw down:   $lan_down"
echo "IPv6 down:     $v6_down   (expected high if WAN IPv6 / docker IPv6 is off)"
[ -n "$tput" ] && echo "last tput:     $tput"
echo
echo "(percentiles need jq, e.g.  jq -s 'map(.targets[0].rtt_ms)|sort' <file>"
echo "                            jq -s 'map(.targets[0].jitter_ms)'   <file> )"
