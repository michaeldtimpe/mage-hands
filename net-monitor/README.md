# net-monitor

An always-on container on **kappa** that logs internet connectivity health, one JSON line every
**10 s**, to per-day files `data/connectivity-YYYY-MM-DD.jsonl`. Lives in this repo for versioning
but is **not** a mage-hands MCP relay — it's standalone home-lab infra. Deployed on kappa at
`/volume1/docker/mage-hands/net-monitor/`. Image is `alpine + curl + iputils` (see `Dockerfile`);
the script is bind-mounted so edits don't need a rebuild.

## What it records (per sample, every `INTERVAL`=10 s)
- `gw_up` / `gw_rtt_ms` / `gw_jitter_ms` / `gw_loss_pct` — LAN → router (192.168.1.1). Isolates a
  kappa↔router problem from an ISP problem.
- `wan_up` + `targets[]` (`rtt_ms`, **`jitter_ms`**, `loss_pct` for 1.1.1.1 and 8.8.8.8) — the WAN
  signal. Jitter is rtt `mdev` from iputils ping (5 pings @ 0.2 s).
- `ipv6_ok` — IPv6 path reachable (pings `TARGET6`). **Expect `false` here:** the router's WAN IPv6
  service is currently disabled *and* the docker bridge is IPv4-only — so this correctly reports "no
  v6 path." It starts passing once WAN IPv6 is enabled (and the docker network is given IPv6).
- `dns_ok` — name resolution works (catches "internet up but nothing loads").
- `tput` *(periodic only)* — `{down_mbps, up_mbps}` via Cloudflare's speed endpoints. **A speedtest
  is heavy, so it runs on its own cadence** (`THROUGHPUT_EVERY`, default 6 h), not every sample.

## Deploy (on kappa, as root)
```sh
cd /volume1/docker/mage-hands/net-monitor
sudo docker compose up -d --build
```

## Use
```sh
tail -F /volume1/docker/mage-hands/net-monitor/data/connectivity-$(date -u +%F).jsonl   # live (today)
sudo docker exec net-monitor sh /app/summary.sh                                          # rollup across all days
sudo docker logs --tail 5 net-monitor                                                    # container health
```

## Storage & retention
The logs **persist on kappa** — they are not ephemeral. The container bind-mounts `./data` to the
host, so every sample lands in a real file on the NAS pool at
`/volume1/docker/mage-hands/net-monitor/data/connectivity-<UTC-date>.jsonl`, surviving container
restart / recreate / rebuild **and** a kappa reboot (`restart: unless-stopped`). One file per UTC
day; files older than `RETAIN_DAYS` (365) are pruned at the daily rollover — so it keeps a **rolling
one-year history** (~0.9 GB steady state). Raise `RETAIN_DAYS` to keep longer.

Note: `data/` is **gitignored** — that keeps runtime logs out of the *git repo*, and has nothing to
do with on-disk retention on kappa (the logs are retained there regardless). To archive beyond the
retention window, copy the per-day files off kappa.

## Alerting
Edge-triggered (fires once on DOWN and once on RECOVERED, not every sample). Disabled until you set
a destination in `compose.yaml`, then `up -d --build`:
- `ALERT_NTFY_URL` — e.g. `https://ntfy.sh/your-secret-topic` (install the ntfy app, subscribe to the
  topic). Simplest.
- `ALERT_WEBHOOK_URL` — generic JSON webhook; POSTs `{"text":"..."}` (Slack-compatible).
- `ALERT_AFTER` (consecutive bad samples before paging, default 3 = ~30 s), `ALERT_REPEAT` (re-page
  interval while down, default 1800 s). Set `ALERT_TEST=1` once to verify delivery at startup.

## Tuning & sizing
Edit `compose.yaml` env, then `sudo docker compose up -d --build`. Key knobs: `INTERVAL`,
`TARGETS`, `TARGET6` (empty disables v6), `THROUGHPUT_EVERY` / `THROUGHPUT_BYTES` /
`THROUGHPUT_UP_BYTES`, `RETAIN_DAYS`.

- **Log size:** ~300 B/line × 6/min ≈ **~0.9 GB/year** (~2.5 MB/day) at 10 s. Per-day files are
  pruned past `RETAIN_DAYS` (365), so on-disk steady state ≈ that figure. Throughput lines add a
  negligible amount.
- **Throughput data cost:** ~`THROUGHPUT_BYTES + THROUGHPUT_UP_BYTES` per run × runs/day. Defaults
  (25 MB + 10 MB, every 6 h) ≈ **~140 MB/day** of test traffic. Lengthen `THROUGHPUT_EVERY` or set
  it to `0` to disable. The first run happens at startup (doubles as a self-test).

**If you edit `monitor.sh` / `summary.sh`,** the running container won't see it (Docker bind-mounts a
single file by inode; rsync replaces the inode). Force a re-mount:
`sudo docker compose -f /volume1/docker/mage-hands/net-monitor/compose.yaml up -d --force-recreate`
(the `data/` logs persist). The container measures from kappa; it can't run if kappa is down.
