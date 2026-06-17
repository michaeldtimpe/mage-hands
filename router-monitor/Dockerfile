# router-monitor image: alpine + the two tools the script needs beyond busybox.
#   openssh-client — SSH out to the ASUS/Merlin router (reuses the router-hands key, read-only)
#   curl           — optional edge alerts (ntfy / webhook); unused when no ALERT_* URL is set
# The script is NOT copied in — it's bind-mounted at runtime so edits don't need a rebuild
# (see compose.yaml). No NET_RAW / no privileged: this only opens an outbound SSH session.
FROM alpine:3.20
RUN apk add --no-cache openssh-client curl
