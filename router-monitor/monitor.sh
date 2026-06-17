#!/bin/sh
# router-monitor — always-on ASUS/Merlin router telemetry logger on kappa.
#
# Complements net-monitor: net-monitor measures the INTERNET path quality from kappa's vantage;
# router-monitor records the ROUTER's own internal state, and — most importantly — mirrors the
# router's /jffs/syslog.log OFF-BOX before its daily rotation discards it, so the next incident
# (e.g. the firmware episode that left no trail) is actually recoverable afterwards.
#
# It SSHes to the router reusing the router-hands key (read-only mount), on a slow cadence, over a
# single multiplexed connection (ControlMaster) so it doesn't spam the router's own auth log.
#
# Outputs under $LOG_DIR (host-readable):
#   health-YYYY-MM-DD.jsonl  one JSON line / $HEALTH_EVERY s (uptime+reboot, load, mem, temp,
#                            conntrack, WAN link+IP, client counts, firmware + update-available)
#   syslog-YYYY-MM-DD.log    the router syslog, mirrored line-exact (rotation-aware: no gaps/dups)
#   events-YYYY-MM-DD.jsonl  edge events: reboot / wan_ip_change / firmware-state / un/reachable
set -u

LOG_DIR="${LOG_DIR:-/data}"
HEALTH_EVERY="${HEALTH_EVERY:-60}"       # health snapshot cadence (s)
SYSLOG_EVERY="${SYSLOG_EVERY:-120}"      # syslog mirror cadence (s); 0 disables the mirror
RETAIN_DAYS="${RETAIN_DAYS:-365}"

ROUTER_HOST="${ROUTER_HOST:-192.168.1.1}"
ROUTER_USER="${ROUTER_USER:-admin}"
ROUTER_PORT="${ROUTER_PORT:-22}"
ROUTER_KEY="${ROUTER_KEY:-/secrets/router_key}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/secrets/known_hosts}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-10}"
SYSLOG_PATH="${SYSLOG_PATH:-/jffs/syslog.log}"

# Alerting (edge-triggered; no destination => disabled, like net-monitor). For-logging-first, so off.
ALERT_NTFY_URL="${ALERT_NTFY_URL:-}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
ALERT_TEST="${ALERT_TEST:-0}"

STATE="$LOG_DIR/.state"
mkdir -p "$LOG_DIR"

# One multiplexed SSH connection reused across cycles (ControlPersist) so we generate ~1 router
# auth-log line / 5 min instead of one per poll. ControlMaster=auto degrades to per-call connects
# if dropbear ever rejects multiplexing.
SSH="ssh -i $ROUTER_KEY -p $ROUTER_PORT -o BatchMode=yes -o ConnectTimeout=$CONNECT_TIMEOUT \
-o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes \
-o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
-o ControlMaster=auto -o ControlPath=/tmp/rm-%r@%h:%p -o ControlPersist=300 \
${ROUTER_USER}@${ROUTER_HOST}"

rsh() { $SSH "$1" 2>/dev/null; }   # run a remote command; stdout only, empty on failure

jnum() { case "${1:-}" in ''|*[!0-9.-]*) echo null;; *) echo "$1";; esac; }
jstr() { [ -z "${1:-}" ] && { echo null; return; }; printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

send_alert() {
  [ -n "$ALERT_NTFY_URL" ]    && curl -s -m 10 -d "$1" "$ALERT_NTFY_URL" >/dev/null 2>&1
  [ -n "$ALERT_WEBHOOK_URL" ] && curl -s -m 10 -H 'Content-Type: application/json' \
        -d "{\"text\":\"$1\"}" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1
  return 0
}
event() {  # event(type, json_extra, human) -> events log (+ optional alert)
  printf '{"ts":"%s","event":"%s"%s}\n' "$1" "$2" "${3:+,$3}" >> "$LOG_DIR/events-$(date -u +%F).jsonl"
}

# Remote health probe — ONE round trip, emits key=value lines. Uses only nvram / absolute /proc
# paths and NO awk/sed (so it never trips the Broadcom rogue-`sh` PATH issue, and quoting stays sane).
REMOTE_HEALTH=$(cat <<'RH'
echo "uptime_s=$(cut -d. -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "mem_total_kb=$(grep -m1 MemTotal /proc/meminfo | tr -dc 0-9)"
echo "mem_avail_kb=$(grep -m1 MemAvailable /proc/meminfo | tr -dc 0-9)"
echo "ct=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null)"
echo "ct_max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null)"
echo "fw=$(nvram get firmver)_$(nvram get buildno)_$(nvram get extendno)"
echo "fw_avail=$(nvram get webs_state_info)"
echo "fw_flag=$(nvram get webs_state_flag)"
echo "wan_state=$(nvram get wan0_state_t)"
echo "wan_ip=$(nvram get wan0_ipaddr)"
echo "wan_proto=$(nvram get wan0_proto)"
echo "dhcp=$(grep -c . /var/lib/misc/dnsmasq.leases 2>/dev/null)"
echo "arp=$(grep -c 0x2 /proc/net/arp 2>/dev/null)"
t=; for z in /sys/class/thermal/thermal_zone*/temp; do [ -f "$z" ] && { t=$(cat "$z"); break; }; done
echo "temp_raw=$t"
RH
)

# Mirror the router syslog to a durable per-day file. Line-count offset keyed on the file inode,
# so it survives daily rotation (syslog.log -> syslog.log-1) with no gaps and no duplicates.
mirror_syslog() {
  [ "${SYSLOG_EVERY:-0}" -gt 0 ] 2>/dev/null || return 0
  meta=$(rsh "I=\$(ls -i $SYSLOG_PATH 2>/dev/null | awk '{print \$1}'); L=\$(wc -l < $SYSLOG_PATH 2>/dev/null); echo \"\$I \$L\"")
  set -- $meta; inode="${1:-}"; lines="${2:-}"
  [ -n "$inode" ] && [ -n "$lines" ] || return 0          # unreachable; leave state untouched
  out="$LOG_DIR/syslog-$(date -u +%F).log"
  if [ "$inode" = "${SL_INODE:-}" ]; then
    if [ "$lines" -ge "${SL_LINES:-0}" ] 2>/dev/null; then
      [ "$lines" -gt "${SL_LINES:-0}" ] && rsh "awk 'NR>${SL_LINES:-0}' $SYSLOG_PATH" >> "$out"
    else
      rsh "cat $SYSLOG_PATH" >> "$out"                    # truncated in place
    fi
  else
    # rotation: flush the tail of the OLD file (now syslog.log-1), then take the whole new file.
    if [ -n "${SL_INODE:-}" ]; then
      oldi=$(rsh "ls -i ${SYSLOG_PATH}-1 2>/dev/null | awk '{print \$1}'")
      [ "$oldi" = "$SL_INODE" ] && rsh "awk 'NR>${SL_LINES:-0}' ${SYSLOG_PATH}-1" >> "$out"
    fi
    rsh "cat $SYSLOG_PATH" >> "$out"
  fi
  SL_INODE="$inode"; SL_LINES="$lines"
}

# ---- state across cycles (we control this file; sourcing it is safe) --------------------------
B_EPOCH=; WAN_IP=; FW_AVAIL=; SL_INODE=; SL_LINES=; REACH=
[ -f "$STATE" ] && . "$STATE"
save_state() {
  { echo "B_EPOCH='$B_EPOCH'"; echo "WAN_IP='$WAN_IP'"; echo "FW_AVAIL='$FW_AVAIL'"
    echo "SL_INODE='$SL_INODE'"; echo "SL_LINES='$SL_LINES'"; echo "REACH='$REACH'"; } > "$STATE"
}

echo "router-monitor: health every ${HEALTH_EVERY}s, syslog every ${SYSLOG_EVERY}s -> $LOG_DIR (router=$ROUTER_USER@$ROUTER_HOST:$ROUTER_PORT)"
echo "  alerts: ntfy=$([ -n "$ALERT_NTFY_URL" ] && echo on || echo off) webhook=$([ -n "$ALERT_WEBHOOK_URL" ] && echo on || echo off)"
[ "$ALERT_TEST" = 1 ] && send_alert "router-monitor test alert $(date -u +%FT%TZ)"

SYSLOG_LAST=0
while :; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ); now=$(date +%s); today=$(date -u +%F)

  H=$($SSH "$REMOTE_HEALTH" 2>/dev/null)

  if [ -z "$H" ] || ! printf '%s' "$H" | grep -q '^uptime_s='; then
    # Router/SSH unreachable — record the gap so an outage is visible in the log.
    printf '{"ts":"%s","reachable":false}\n' "$ts" >> "$LOG_DIR/health-$today.jsonl"
    [ "${REACH:-up}" != down ] && { event "$ts" "router_unreachable" ""; send_alert "[router-monitor] router UNREACHABLE at $ts"; }
    REACH=down; save_state; sleep "$HEALTH_EVERY"; continue
  fi

  uptime_s=; load=; mem_total_kb=; mem_avail_kb=; ct=; ct_max=; fw=; fw_avail=; fw_flag=
  wan_state=; wan_ip=; wan_proto=; dhcp=; arp=; temp_raw=
  while IFS='=' read -r k v; do
    case "$k" in
      uptime_s) uptime_s=$v;; load) load=$v;; mem_total_kb) mem_total_kb=$v;; mem_avail_kb) mem_avail_kb=$v;;
      ct) ct=$v;; ct_max) ct_max=$v;; fw) fw=$v;; fw_avail) fw_avail=$v;; fw_flag) fw_flag=$v;;
      wan_state) wan_state=$v;; wan_ip) wan_ip=$v;; wan_proto) wan_proto=$v;; dhcp) dhcp=$v;;
      arp) arp=$v;; temp_raw) temp_raw=$v;;
    esac
  done <<EOF
$H
EOF

  # derived
  set -- $load; load1="${1:-}"; load5="${2:-}"; load15="${3:-}"
  mem_used_pct=$(awk -v t="${mem_total_kb:-0}" -v a="${mem_avail_kb:-0}" 'BEGIN{if(t>0)printf "%.1f",(t-a)*100/t; else print "null"}')
  ct_pct=$(awk -v c="${ct:-0}" -v m="${ct_max:-0}" 'BEGIN{if(m>0)printf "%.1f",c*100/m; else print "null"}')
  temp_c=$(awk -v r="${temp_raw:-}" 'BEGIN{if(r=="")print "null"; else if(r+0>1000)printf "%.1f",r/1000; else printf "%.1f",r}')
  [ "${wan_state:-}" = 2 ] && wan_up=true || wan_up=false
  [ "${fw_flag:-0}" = 1 ] && fw_pending=true || fw_pending=false

  boot=$((now - ${uptime_s:-0})); reboot=false
  if [ -n "${B_EPOCH:-}" ] && [ $((boot - B_EPOCH)) -gt 120 ] 2>/dev/null; then reboot=true; fi
  B_EPOCH=$boot

  wan_changed=false
  if [ -n "${WAN_IP:-}" ] && [ "$WAN_IP" != "${wan_ip:-}" ]; then wan_changed=true; fi

  printf '{"ts":"%s","reachable":true,"uptime_s":%s,"boot_epoch":%s,"reboot":%s,"load1":%s,"load5":%s,"load15":%s,"mem_total_kb":%s,"mem_avail_kb":%s,"mem_used_pct":%s,"conntrack":%s,"conntrack_max":%s,"conntrack_pct":%s,"temp_c":%s,"wan_up":%s,"wan_state":%s,"wan_ip":%s,"wan_ip_changed":%s,"wan_proto":%s,"dhcp_leases":%s,"arp_reachable":%s,"fw":%s,"fw_avail":%s,"fw_update_pending":%s}\n' \
    "$ts" "$(jnum "$uptime_s")" "$(jnum "$boot")" "$reboot" "$(jnum "$load1")" "$(jnum "$load5")" "$(jnum "$load15")" \
    "$(jnum "$mem_total_kb")" "$(jnum "$mem_avail_kb")" "${mem_used_pct}" "$(jnum "$ct")" "$(jnum "$ct_max")" "${ct_pct}" \
    "${temp_c}" "$wan_up" "$(jnum "$wan_state")" "$(jstr "$wan_ip")" "$wan_changed" "$(jstr "$wan_proto")" \
    "$(jnum "$dhcp")" "$(jnum "$arp")" "$(jstr "$fw")" "$(jstr "$fw_avail")" "$fw_pending" \
    >> "$LOG_DIR/health-$today.jsonl"

  # edge events
  [ "${REACH:-}" = down ] && { event "$ts" "router_reachable" "\"uptime_s\":$(jnum "$uptime_s")"; send_alert "[router-monitor] router back at $ts"; }
  REACH=up
  [ "$reboot" = true ]      && { event "$ts" "reboot" "\"uptime_s\":$(jnum "$uptime_s")"; send_alert "[router-monitor] router REBOOTED (uptime ${uptime_s}s) at $ts"; }
  [ "$wan_changed" = true ] && { event "$ts" "wan_ip_change" "\"from\":$(jstr "$WAN_IP"),\"to\":$(jstr "$wan_ip")"; send_alert "[router-monitor] WAN IP $WAN_IP -> $wan_ip at $ts"; }
  if [ -n "${FW_AVAIL:-}" ] && [ "$FW_AVAIL" != "${fw_avail:-}" ]; then
    event "$ts" "firmware_state_change" "\"from\":$(jstr "$FW_AVAIL"),\"to\":$(jstr "$fw_avail")"
    send_alert "[router-monitor] firmware-available changed: $FW_AVAIL -> $fw_avail at $ts"
  fi
  WAN_IP="${wan_ip:-}"; FW_AVAIL="${fw_avail:-}"

  # syslog mirror on its own cadence
  if [ "${SYSLOG_EVERY:-0}" -gt 0 ] 2>/dev/null && [ $((now - SYSLOG_LAST)) -ge "$SYSLOG_EVERY" ]; then
    mirror_syslog; SYSLOG_LAST=$now
  fi

  save_state

  # retention prune once per day rollover (cheap)
  if [ "${DAYMARK:-}" != "$today" ]; then
    DAYMARK=$today
    find "$LOG_DIR" -name 'health-*.jsonl' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null
    find "$LOG_DIR" -name 'events-*.jsonl' -mtime +"$RETAIN_DAYS" -delete 2>/dev/null
    find "$LOG_DIR" -name 'syslog-*.log'   -mtime +"$RETAIN_DAYS" -delete 2>/dev/null
  fi

  sleep "$HEALTH_EVERY"
done
