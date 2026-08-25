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
- `ipv6_ok` — IPv6 path reachable (pings `TARGET6`). The container runs `network_mode: host` (see
  Tuning & sizing below), so it inherits kappa's own IPv6 stack — **expect `true`**. It logged
  `false` for the entire history through 2026-08-24 purely because the *docker bridge* network was
  IPv4-only; the host itself always had working WAN/LAN IPv6. See `lessons.md` for the AT&T
  IPv4-vs-IPv6 routing fault that discontinuity ended up helping to prove.
- `dns_ok` — name resolution works (catches "internet up but nothing loads").
- `tput` *(periodic only)* — `{down_mbps, up_mbps, cf_down_mbps, cf6_down_mbps, alt_down_mbps,
  bytes, streams}` via Cloudflare's speed endpoints plus an optional alternate target. **A
  speedtest is heavy, so it runs once a day** rather than every sample — see `THROUGHPUT_HOUR`
  and `THROUGHPUT_EVERY` under Tuning & sizing. `down_mbps` is the **best of the three measured
  paths** (Cloudflare-v4, Cloudflare-v6, and the alternate target if `THROUGHPUT_ALT_URL` is
  set) — a deliberate change from earlier records, where it meant Cloudflare-v4 only. `bytes` and
  `streams` make each record self-describing so that discontinuity stays legible when scanning
  history; `up_mbps` keeps its original single-path Cloudflare meaning. **Measurement ceiling:**
  kappa has a 1 GbE NIC, so no path here can ever report above ~940 Mbps — a reading at that
  number means "at least gigabit," not "exactly gigabit." **Timestamps stay UTC:** the once-daily
  scheduling decision (below) reads local time, but the `ts` field and the per-day filename it
  lands in are UTC as always — don't read a local-time run trigger as a change in log timezone.
  **Unverified risk (noted 2026-08-24):** `speed.cloudflare.com/__down` returned **HTTP 403** to
  macOS `curl` from another host that day, with and without a browser User-Agent. This was *not*
  reproduced from kappa or from inside this container — alpine's curl presents a different TLS
  fingerprint and UA, so it may well be unaffected. But if `cf_down_mbps`/`cf6_down_mbps` ever go
  null or implausibly low, check for a 403 before assuming the link degraded. Linode Dallas
  (`speedtest.dallas.linode.com/garbage.php?ckSize=2048`) answered normally and is a usable
  fallback target.

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
`TARGETS`, `TARGET6` (empty disables v6), `THROUGHPUT_HOUR` / `THROUGHPUT_EVERY` /
`THROUGHPUT_BYTES` / `THROUGHPUT_STREAMS` / `THROUGHPUT_UP_BYTES` / `THROUGHPUT_ALT_URL`,
`RETAIN_DAYS`.

- `THROUGHPUT_HOUR` (default `4`) — the throughput test runs once per **local** day, at or after
  this hour (`>=`, not `==`), so a container that was down at 4 AM still catches up later that
  same day instead of skipping it entirely). `THROUGHPUT_EVERY` (default `86400`) is the fallback
  interval and the disable switch (`0` disables the test, same as before). Local-day scheduling
  needs a real timezone in the container: `compose.yaml` sets `TZ=US/Central` (kappa's own zone),
  and that **requires `tzdata` in the image** — Alpine ships without it, and an unset `TZ` package
  makes `TZ` silently do nothing (verified: the container ran UTC with no zoneinfo before this was
  added). If a rebuild ever drops `tzdata`, `THROUGHPUT_HOUR` will silently stop firing at the
  intended local hour rather than erroring.
- **Timestamps stay UTC regardless.** Only the once-daily scheduling *decision* reads local time;
  the JSONL filenames and every record's `ts` field are UTC, unchanged from before.
- `THROUGHPUT_STREAMS` (default `4`) — parallel connections used per measured path. A single
  stream slow-starts and undercounts a gigabit link at typical internet RTTs; multiple streams in
  parallel are what let the test actually approach the link's real ceiling.
- `THROUGHPUT_ALT_URL` (default empty, optional) — a non-Cloudflare target measured alongside
  Cloudflare's v4/v6 endpoints, so a Cloudflare-specific routing anomaly shows up as a gap against
  `alt_down_mbps` instead of masquerading as a link problem. Empty disables that path;
  `alt_down_mbps` is then always `null` in the log. **Stays empty by default** — the candidate
  targets evaluated (an aurora VM, two Hetzner public speed hosts) were all rejected as too
  shaped by their own hosting to be a useful reference; see `lessons.md`. The cf-v4-vs-cf-v6
  comparison is the discriminator that actually matters, and it costs nothing extra.
- **Host networking:** the container runs `network_mode: host` (no ports published) instead of a
  bridge network. This is required to measure the IPv6 path at all — giving the docker *bridge*
  IPv6 needs `"ipv6": true` in `/etc/docker/daemon.json` plus a dockerd restart, which on kappa
  would kill every other container on the box (see `lessons.md`). Host networking borrows kappa's
  already-working IPv6 stack with zero daemon changes and no restart risk. `cap_add: NET_RAW` is
  still required for ping.
- **Log size:** ~300 B/line × 6/min ≈ **~0.9 GB/year** (~2.5 MB/day) at 10 s. Per-day files are
  pruned past `RETAIN_DAYS` (365), so on-disk steady state ≈ that figure. Throughput lines add a
  negligible amount.
- **Throughput data cost:** one run/day, two download paths (Cloudflare v4 + v6) plus one upload,
  each at `THROUGHPUT_STREAMS=4`. At defaults (`THROUGHPUT_BYTES=75000000`,
  `THROUGHPUT_UP_BYTES=10000000`) that's cf `4×75 MB = 300 MB` + cf6 `4×75 MB = 300 MB` + upload
  `4×10 MB = 40 MB` = **640 MB/run**, and at once-daily cadence that's **~640 MB/day
  (~234 GB/yr)** of test traffic — up from ~140 MB/day before the multi-path rewrite. (`alt`
  adds nothing to this by default — see `THROUGHPUT_ALT_URL` above.) Lower `THROUGHPUT_STREAMS`
  or `THROUGHPUT_BYTES`, or set `THROUGHPUT_EVERY=0` to disable, to reduce further. In hour mode
  there is **no unconditional run-at-startup self-test**: because the hour test is `>=`, a
  container started between `THROUGHPUT_HOUR` and 23:59 local runs immediately (the catch-up
  path), but one started between 00:00 and `THROUGHPUT_HOUR` sits idle until that hour. Deleting
  `data/.tput_last` forces a run subject to the same window.

**If you edit `monitor.sh` / `summary.sh`,** the running container won't see it (Docker bind-mounts a
single file by inode; rsync replaces the inode). Force a re-mount:
`sudo docker compose -f /volume1/docker/mage-hands/net-monitor/compose.yaml up -d --force-recreate`
(the `data/` logs persist). The container measures from kappa; it can't run if kappa is down.
