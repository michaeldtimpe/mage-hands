# net-monitor

A tiny always-on container on **kappa** that logs internet connectivity health, one JSON line
per minute, to `data/connectivity.jsonl`. Lives in this repo for versioning but is **not** a
mage-hands MCP relay — it's standalone home-lab infra. Deployed on kappa at
`/volume1/docker/mage-hands/net-monitor/`.

## What it records (per sample)
- `gw_up` / `gw_rtt_ms` / `gw_loss_pct` — LAN → router (192.168.1.1). Isolates a kappa↔router
  problem from an ISP problem.
- `wan_up` + `targets[]` (`rtt_ms`, `loss_pct` for 1.1.1.1 and 8.8.8.8) — the WAN/ISP signal.
- `dns_ok` — name resolution works (catches "internet up but nothing loads").

## Deploy (on kappa, as root)
```sh
cd /volume1/docker/mage-hands/net-monitor
sudo docker compose up -d
```

## Use
```sh
tail -f /volume1/docker/mage-hands/net-monitor/data/connectivity.jsonl   # live
sudo docker exec net-monitor sh /app/summary.sh                          # uptime / outage / DNS summary
sudo docker logs --tail 5 net-monitor                                    # container health
```

## Tuning
Edit `compose.yaml` env then `sudo docker compose up -d` again (a config change auto-recreates):
`INTERVAL` (sec), `GATEWAY`, `TARGETS` (space-separated), `DNS_NAME`. The log self-trims at
`MAX_LINES` (~1 year @ 1/min). Goes out the normal route, so it measures the same path the LAN uses.

**If you edit `monitor.sh` / `summary.sh`,** the running container won't see it (Docker bind-mounts
a single file by inode; copying/rsync replaces the inode). Force a re-mount:
`sudo docker compose -f /volume1/docker/mage-hands/net-monitor/compose.yaml up -d --force-recreate`
(the `data/` log persists). The container measures from kappa; it can't run if kappa is down.
