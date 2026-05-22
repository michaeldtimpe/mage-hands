# net-monitor image: alpine + the two tools the script needs beyond busybox.
#   curl     — throughput (Cloudflare speed endpoints) + alert POSTs (ntfy/webhook)
#   iputils  — ping with sub-second -i and rtt mdev (true jitter; busybox ping has neither)
# The scripts are NOT copied in — they're bind-mounted at runtime so edits don't need a rebuild
# (see compose.yaml). cap_add: NET_RAW (in compose) lets ping open raw sockets.
FROM alpine:3.20
RUN apk add --no-cache curl iputils
