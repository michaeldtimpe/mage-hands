#!/bin/sh
# Internet connectivity health logger — always-on container on kappa.
# Fast loop (every $INTERVAL s) writes ONE JSON line per sample to a PER-DAY file
# ($LOG_DIR/connectivity-YYYY-MM-DD.jsonl). A slow throughput test and edge-triggered
# alerting piggyback on the same loop. Needs: busybox + iputils ping + curl (see Dockerfile).
#
# Layers per sample (so an outage is diagnosable, not just "internet down"):
#   gw_*      LAN -> router (kappa<->router vs ISP)
#   wan_*     router -> internet (ping v4 targets) + per-target rtt/jitter/loss
#   ipv6_ok   IPv6 path reachable (false if no v6 on the WAN or the docker net)
#   dns_ok    name resolution works
#   tput      (periodic) down/up Mbps via Cloudflare's speed endpoints
set -u

LOG_DIR="${LOG_DIR:-/data}"
INTERVAL="${INTERVAL:-10}"
GATEWAY="${GATEWAY:-192.168.1.1}"
TARGETS="${TARGETS:-1.1.1.1 8.8.8.8}"          # WAN v4 ping targets
TARGET6="${TARGET6:-2606:4700:4700::1111}"      # WAN v6 target (Cloudflare); empty to skip
DNS_NAME="${DNS_NAME:-google.com}"
PING_COUNT="${PING_COUNT:-5}"
PING_INT="${PING_INT:-0.2}"                      # sub-second needs iputils ping (we install it)
RETAIN_DAYS="${RETAIN_DAYS:-365}"

# Throughput (own cadence — a speedtest is heavy; NEVER run it every $INTERVAL).
THROUGHPUT_EVERY="${THROUGHPUT_EVERY:-21600}"    # seconds between speedtests (0 disables); 6h default
THROUGHPUT_BYTES="${THROUGHPUT_BYTES:-25000000}" # download size (~25 MB)
THROUGHPUT_UP_BYTES="${THROUGHPUT_UP_BYTES:-10000000}"  # upload size (~10 MB; 0 disables upload)

# Alerting (edge-triggered; no destination set => disabled). ntfy = plain POST body; webhook = JSON {"text":...}.
ALERT_NTFY_URL="${ALERT_NTFY_URL:-}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
ALERT_AFTER="${ALERT_AFTER:-3}"                  # consecutive bad samples before paging (3 = ~30s)
ALERT_REPEAT="${ALERT_REPEAT:-1800}"             # re-page every N s while still down (0 = once)
ALERT_TEST="${ALERT_TEST:-0}"                    # set 1 to send a test alert at startup

STATE="$LOG_DIR/.alert_state"
TPUT_LAST="$LOG_DIR/.tput_last"
DAYMARK="$LOG_DIR/.day"
mkdir -p "$LOG_DIR"

# ping HOST [proto] -> "avg jitter loss"  (avg/jitter = "na" if unreachable; loss is %).
# Parses BOTH iputils (min/avg/max/mdev) and busybox (min/avg/max -> jitter=max-min).
probe() {
  out=$(ping ${2:-} -c "$PING_COUNT" -i "$PING_INT" -W 2 -q "$1" 2>/dev/null)
  loss=$(echo "$out" | sed -n 's/.* \([0-9]*\)% packet loss.*/\1/p')
  stats=$(echo "$out" | sed -n 's#.*= \([0-9./]*\) ms#\1#p')   # a/b/c[/d]
  oIFS=$IFS; IFS=/; set -- $stats; IFS=$oIFS
  avg="${2:-}"
  if [ -n "${4:-}" ]; then jit="$4"
  elif [ -n "${3:-}" ] && [ -n "${1:-}" ]; then jit=$(awk -v x="$3" -v n="$1" 'BEGIN{printf "%.3f", x-n}')
  else jit=""; fi
  echo "${avg:-na} ${jit:-na} ${loss:-100}"
}
probe_bg() { ( probe "$2" "$3" > "/tmp/pr_$1" ) & }
jnum() { [ "$1" = na ] && echo null || echo "$1"; }

send_alert() {
  [ -n "$ALERT_NTFY_URL" ]    && curl -s -m 10 -d "$1" "$ALERT_NTFY_URL" >/dev/null 2>&1
  [ -n "$ALERT_WEBHOOK_URL" ] && curl -s -m 10 -H 'Content-Type: application/json' \
        -d "{\"text\":\"$1\"}" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1
}

throughput() {   # echoes "down_mbps up_mbps"
  dn=$(curl -s -o /dev/null --max-time 30 -w '%{speed_download}' \
        "https://speed.cloudflare.com/__down?bytes=$THROUGHPUT_BYTES" 2>/dev/null)
  d=$(awk -v b="${dn:-0}" 'BEGIN{printf "%.1f", b*8/1000000}')
  u="null"
  if [ "${THROUGHPUT_UP_BYTES:-0}" -gt 0 ] 2>/dev/null; then
    ub=$(head -c "$THROUGHPUT_UP_BYTES" /dev/zero | curl -s -o /dev/null --max-time 30 \
          -w '%{speed_upload}' --data-binary @- "https://speed.cloudflare.com/__up" 2>/dev/null)
    u=$(awk -v b="${ub:-0}" 'BEGIN{printf "%.1f", b*8/1000000}')
  fi
  echo "$d $u"
}

echo "net-monitor: every ${INTERVAL}s -> $LOG_DIR/connectivity-<date>.jsonl (gw=$GATEWAY v4='$TARGETS' v6='$TARGET6')"
echo "  throughput every ${THROUGHPUT_EVERY}s; alerts: ntfy=$([ -n "$ALERT_NTFY_URL" ] && echo on || echo off) webhook=$([ -n "$ALERT_WEBHOOK_URL" ] && echo on || echo off)"
[ "$ALERT_TEST" = 1 ] && send_alert "net-monitor test alert $(date -u +%FT%TZ)"

while :; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ); now=$(date +%s)

  # parallel probes (keeps an iteration well under $INTERVAL even with a slow/timing-out v6)
  probe_bg gw "$GATEWAY" ""
  n=0; for t in $TARGETS; do probe_bg "v4_$n" "$t" ""; n=$((n + 1)); done
  [ -n "$TARGET6" ] && probe_bg v6 "$TARGET6" "-6"
  ( nslookup "$DNS_NAME" >/dev/null 2>&1 && echo ok || echo no ) > /tmp/pr_dns &
  wait

  read gw_avg gw_jit gw_loss < /tmp/pr_gw
  [ "$gw_loss" -lt 100 ] 2>/dev/null && gw_up=true || gw_up=false

  wan_up=false; targets_json=""; i=0
  for t in $TARGETS; do
    read avg jit loss < "/tmp/pr_v4_$i"
    [ "$loss" -lt 100 ] 2>/dev/null && wan_up=true
    targets_json="${targets_json:+$targets_json,}{\"target\":\"$t\",\"rtt_ms\":$(jnum "$avg"),\"jitter_ms\":$(jnum "$jit"),\"loss_pct\":$loss}"
    i=$((i + 1))
  done

  ipv6_ok=false
  if [ -n "$TARGET6" ] && [ -f /tmp/pr_v6 ]; then
    read v6_avg v6_jit v6_loss < /tmp/pr_v6
    [ "$v6_loss" -lt 100 ] 2>/dev/null && ipv6_ok=true
  fi

  [ "$(cat /tmp/pr_dns 2>/dev/null)" = ok ] && dns_ok=true || dns_ok=false

  # periodic throughput
  tlast=$(cat "$TPUT_LAST" 2>/dev/null || echo 0)
  tput_json=""
  if [ "$THROUGHPUT_EVERY" -gt 0 ] 2>/dev/null && [ $((now - tlast)) -ge "$THROUGHPUT_EVERY" ]; then
    set -- $(throughput)
    tput_json=",\"tput\":{\"down_mbps\":${1:-null},\"up_mbps\":${2:-null}}"
    echo "$now" > "$TPUT_LAST"
  fi

  today=$(date -u +%F)
  printf '{"ts":"%s","gw_up":%s,"gw_rtt_ms":%s,"gw_jitter_ms":%s,"gw_loss_pct":%s,"wan_up":%s,"ipv6_ok":%s,"dns_ok":%s,"targets":[%s]%s}\n' \
    "$ts" "$gw_up" "$(jnum "$gw_avg")" "$(jnum "$gw_jit")" "$gw_loss" "$wan_up" "$ipv6_ok" "$dns_ok" "$targets_json" "$tput_json" \
    >> "$LOG_DIR/connectivity-$today.jsonl"

  # day rollover: prune archives past retention (cheap, runs once/day)
  if [ "$(cat "$DAYMARK" 2>/dev/null)" != "$today" ]; then
    echo "$today" > "$DAYMARK"
    find "$LOG_DIR" -name 'connectivity-*.jsonl' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null
  fi

  # edge-triggered alerting on WAN reachability
  if [ -f "$STATE" ]; then read st consec last < "$STATE"; else st=UP; consec=0; last=0; fi
  [ -n "${st:-}" ] || st=UP; [ -n "${consec:-}" ] || consec=0; [ -n "${last:-}" ] || last=0
  if [ "$wan_up" = false ]; then
    consec=$((consec + 1))
    if [ "$st" = UP ] && [ "$consec" -ge "$ALERT_AFTER" ]; then
      send_alert "[net-monitor] internet DOWN at $ts (gw_up=$gw_up dns_ok=$dns_ok)"; st=DOWN; last=$now
    elif [ "$st" = DOWN ] && [ "$ALERT_REPEAT" -gt 0 ] 2>/dev/null && [ $((now - last)) -ge "$ALERT_REPEAT" ]; then
      send_alert "[net-monitor] still DOWN at $ts"; last=$now
    fi
  else
    [ "$st" = DOWN ] && send_alert "[net-monitor] RECOVERED at $ts"
    st=UP; consec=0
  fi
  echo "$st $consec $last" > "$STATE"

  sleep "$INTERVAL"
done
