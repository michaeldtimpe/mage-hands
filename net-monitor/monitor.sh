#!/bin/sh
# Internet connectivity health logger — always-on container on kappa.
# Fast loop (every $INTERVAL s) writes ONE JSON line per sample to a PER-DAY file
# ($LOG_DIR/connectivity-YYYY-MM-DD.jsonl). A slow throughput test and edge-triggered
# alerting piggyback on the same loop. Needs: busybox + iputils ping + curl (see Dockerfile).
#
# Layers per sample (so an outage is diagnosable, not just "internet down"):
#   gw_*      LAN -> router (kappa<->router vs ISP)
#   wan_*     router -> internet (ping v4 targets) + per-target rtt/jitter/loss
#   ipv6_ok   IPv6 path reachable (false if no v6 on the WAN or on this container's net)
#   dns_ok    name resolution works
#   tput      (daily at THROUGHPUT_HOUR local) down/up Mbps; download on 3 paths (cf v4, cf v6, alt)
set -u

LOG_DIR="${LOG_DIR:-/data}"
INTERVAL="${INTERVAL:-10}"
GATEWAY="${GATEWAY:-192.168.1.1}"
TARGETS="${TARGETS:-1.1.1.1 8.8.8.8}"          # WAN v4 ping targets
TARGET6="${TARGET6:-2606:4700:4700::1001}"      # WAN v6 target (Cloudflare); empty to skip.
                                                 # NOT ::1111 — it answers DNS/HTTPS but drops
                                                 # ICMPv6 echo, so ipv6_ok reads false forever.
DNS_NAME="${DNS_NAME:-google.com}"
PING_COUNT="${PING_COUNT:-5}"
PING_INT="${PING_INT:-0.2}"                      # sub-second needs iputils ping (we install it)
RETAIN_DAYS="${RETAIN_DAYS:-365}"

# Throughput (own cadence — a speedtest is heavy; NEVER run it every $INTERVAL).
THROUGHPUT_HOUR="${THROUGHPUT_HOUR-4}"           # LOCAL hour 0-23 for the once-a-day run (needs TZ + tzdata). "-" not ":-": setting it EMPTY means interval mode
THROUGHPUT_EVERY="${THROUGHPUT_EVERY:-21600}"    # 0 DISABLES throughput entirely; in interval mode, seconds between speedtests
THROUGHPUT_BYTES="${THROUGHPUT_BYTES:-25000000}" # download size (~25 MB)
THROUGHPUT_UP_BYTES="${THROUGHPUT_UP_BYTES:-10000000}"  # upload size (~10 MB; 0 disables upload)
THROUGHPUT_STREAMS="${THROUGHPUT_STREAMS:-4}"    # parallel curl streams per path (1 stream under-measures a fast, high-RTT link)
THROUGHPUT_ALT_URL="${THROUGHPUT_ALT_URL:-}"     # optional non-Cloudflare download URL (empty => alt_down_mbps is null)
[ "$THROUGHPUT_STREAMS" -ge 1 ] 2>/dev/null || THROUGHPUT_STREAMS=4   # keep it numeric: it is emitted into the JSON

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

# tput_one URL IPFLAG BYTES STREAMS [up] -> aggregate Mbps across STREAMS, or "na".
# Same fan-out idiom as probe_bg: N background curls, one temp file each, then wait + sum.
# IPFLAG (-4/-6) is ALWAYS pinned — if curl picks the family, the v4-vs-v6 comparison collapses.
# BYTES only scales --max-time; a timed-out transfer is still a valid sample because curl
# reports the speed it did achieve, and the cap keeps a hung path from stalling the loop.
tput_one() {
  [ -n "$1" ] || { echo na; return; }
  tp_n="$4"; [ "$tp_n" -ge 1 ] 2>/dev/null || tp_n=1
  tp_mt=$(awk -v b="${3:-0}" 'BEGIN{t=10+b/2500000; if(t>60)t=60; if(t<10)t=10; printf "%d", t}')
  rm -f /tmp/tp_s* 2>/dev/null
  tp_i=0
  while [ "$tp_i" -lt "$tp_n" ]; do
    if [ "${5:-}" = up ]; then
      ( head -c "$3" /dev/zero | curl -s "$2" -o /dev/null --max-time "$tp_mt" \
          -w '%{speed_upload}\n' --data-binary @- "$1" > "/tmp/tp_s$tp_i" 2>/dev/null ) &
    else
      ( curl -s "$2" -o /dev/null --max-time "$tp_mt" \
          -w '%{speed_download}\n' "$1" > "/tmp/tp_s$tp_i" 2>/dev/null ) &
    fi
    tp_i=$((tp_i + 1))
  done
  wait
  cat /tmp/tp_s* 2>/dev/null | awk '{s+=$1} END{if(s>0) printf "%.1f", s*8/1000000; else print "na"}'
}

# echoes "best_down up cf cf6 alt" (Mbps each, or "na" -> emitted as JSON null).
# best_down = max of the three download paths, i.e. what the link can actually do.
throughput() {
  tp_url="https://speed.cloudflare.com/__down?bytes=$THROUGHPUT_BYTES"
  tp_cf=$(tput_one  "$tp_url" -4 "$THROUGHPUT_BYTES" "$THROUGHPUT_STREAMS")
  tp_cf6=$(tput_one "$tp_url" -6 "$THROUGHPUT_BYTES" "$THROUGHPUT_STREAMS")
  tp_alt=$(tput_one "$THROUGHPUT_ALT_URL" -4 "$THROUGHPUT_BYTES" "$THROUGHPUT_STREAMS")
  tp_best=$(awk -v a="$tp_cf" -v b="$tp_cf6" -v c="$tp_alt" 'BEGIN{
      m = -1
      if (a != "na" && a+0 > m) m = a+0
      if (b != "na" && b+0 > m) m = b+0
      if (c != "na" && c+0 > m) m = c+0
      if (m < 0) print "na"; else printf "%.1f", m }')
  tp_up=na
  if [ "${THROUGHPUT_UP_BYTES:-0}" -gt 0 ] 2>/dev/null; then
    tp_up=$(tput_one "https://speed.cloudflare.com/__up" -4 "$THROUGHPUT_UP_BYTES" \
              "$THROUGHPUT_STREAMS" up)
  fi
  echo "$tp_best $tp_up $tp_cf $tp_cf6 $tp_alt"
}

# Is a throughput run due? echoes the value to stamp into $TPUT_LAST, or nothing.
# Two modes, both switched OFF by THROUGHPUT_EVERY=0:
#   hour mode (default) — once per LOCAL day, at or after THROUGHPUT_HOUR. ">=", not "==",
#     so a container that was down at the hour still catches up later the same day; the key
#     stored is the local date, so a restart mid-day can't re-run and a DST shift can't
#     double-run. This is the ONLY place local time is read — logs stay UTC.
#   interval mode — the old "$THROUGHPUT_EVERY seconds since the stored epoch".
tput_hour_mode() { [ "${THROUGHPUT_HOUR:-x}" -ge 0 ] 2>/dev/null && [ "$THROUGHPUT_HOUR" -le 23 ] 2>/dev/null; }
tput_due() {   # $1 = now (epoch)
  [ "$THROUGHPUT_EVERY" -gt 0 ] 2>/dev/null || return 0
  tp_last=$(cat "$TPUT_LAST" 2>/dev/null || echo 0)
  if tput_hour_mode; then
    tp_day=$(date +%Y-%m-%d); tp_hr=$(date +%H); tp_hr="${tp_hr#0}"; [ -n "$tp_hr" ] || tp_hr=0
    [ "$tp_last" != "$tp_day" ] && [ "$tp_hr" -ge "$THROUGHPUT_HOUR" ] && echo "$tp_day"
  else
    case "$tp_last" in ""|*[!0-9]*) tp_last=0;; esac   # a leftover date key => "never ran"
    [ $(($1 - tp_last)) -ge "$THROUGHPUT_EVERY" ] && echo "$1"
  fi
  return 0
}
tput_sched_desc() {
  if [ "$THROUGHPUT_EVERY" -gt 0 ] 2>/dev/null; then
    if tput_hour_mode; then echo "daily at ${THROUGHPUT_HOUR}:00 local ($(date +%Z), now $(date +%H:%M))"
    else echo "every ${THROUGHPUT_EVERY}s"; fi
  else echo "disabled"; fi
}

echo "net-monitor: every ${INTERVAL}s -> $LOG_DIR/connectivity-<date>.jsonl (gw=$GATEWAY v4='$TARGETS' v6='$TARGET6')"
echo "  throughput $(tput_sched_desc) (${THROUGHPUT_BYTES}B x ${THROUGHPUT_STREAMS} streams, alt=\"${THROUGHPUT_ALT_URL}\"); alerts: ntfy=$([ -n "$ALERT_NTFY_URL" ] && echo on || echo off) webhook=$([ -n "$ALERT_WEBHOOK_URL" ] && echo on || echo off)"
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

  # periodic throughput (schedule lives in tput_due; local time is read there and NOWHERE else)
  tput_json=""; tmark=$(tput_due "$now")
  if [ -n "$tmark" ]; then
    set -- $(throughput)   # best_down up cf cf6 alt
    # FLAT object only: summary.sh parses it with sed up to the first "}".
    tput_json=",\"tput\":{\"down_mbps\":$(jnum "${1:-na}"),\"up_mbps\":$(jnum "${2:-na}")"
    tput_json="$tput_json,\"cf_down_mbps\":$(jnum "${3:-na}"),\"cf6_down_mbps\":$(jnum "${4:-na}")"
    tput_json="$tput_json,\"alt_down_mbps\":$(jnum "${5:-na}")"
    tput_json="$tput_json,\"bytes\":$THROUGHPUT_BYTES,\"streams\":$THROUGHPUT_STREAMS}"
    echo "$tmark" > "$TPUT_LAST"
  fi

  today=$(date -u +%F)
  printf '{"ts":"%s","gw_up":%s,"gw_rtt_ms":%s,"gw_jitter_ms":%s,"gw_loss_pct":%s,"wan_up":%s,"ipv6_ok":%s,"dns_ok":%s,"targets":[%s]%s}\n' \
    "$ts" "$gw_up" "$(jnum "$gw_avg")" "$(jnum "$gw_jit")" "$gw_loss" "$wan_up" "$ipv6_ok" "$dns_ok" "$targets_json" "$tput_json" \
    >> "$LOG_DIR/connectivity-$today.jsonl"

  # day rollover: prune archives past retention (cheap, runs once/day)
  if [ "$(cat "$DAYMARK" 2>/dev/null)" != "$today" ]; then
    echo "$today" > "$DAYMARK"
    # glob is 'connectivity-*' (not '*.jsonl'): migrations leave sidecars such as
    # connectivity-<date>.jsonl.prefix-bak, and a .jsonl-anchored pattern never ages them out.
    # Safe because every state file in LOG_DIR is a dotfile (.day/.tput_last/.alert_state).
    find "$LOG_DIR" -name 'connectivity-*' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null
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
