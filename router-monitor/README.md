# router-monitor

Always-on telemetry logger for the ASUS Asuswrt-Merlin router (`router1`, RT-AX88U Pro), running
as a container on **kappa**. It is the router-internal complement to [net-monitor](../net-monitor/):

| | net-monitor | **router-monitor** |
|---|---|---|
| Vantage | kappa → internet | kappa → **the router itself** (SSH) |
| Answers | "is the internet up / fast?" | "what is the **router** doing, and what did its log say?" |
| Key output | `connectivity-*.jsonl` | `health-*.jsonl` + **`syslog-*.log`** + `events-*.jsonl` |

**Why this exists:** when the router needed a firmware downgrade a couple months ago it left **no
trail** — the router's `/jffs/syslog.log` rotates roughly daily and is gone, and net-monitor only
saw "internet quality," not the router's state. router-monitor fixes that by recording the router's
internal health *and* **mirroring its syslog off-box before rotation discards it**, so the next
incident is actually diagnosable after the fact.

## What it records

Under `data/` (host-readable, per-day, pruned after `RETAIN_DAYS`):

- **`health-YYYY-MM-DD.jsonl`** — one JSON line per `HEALTH_EVERY` s: `uptime_s` + `reboot` flag,
  `boot_epoch`, `load1/5/15`, `mem_*` + `mem_used_pct`, `conntrack`(`_max`/`_pct`), `temp_c`
  (best-effort; null on SoCs with no `thermal_zone`), `wan_up`/`wan_state`/`wan_ip`(+`_changed`)/
  `wan_proto`, `dhcp_leases`, `arp_reachable`, `fw`, `fw_avail`, `fw_update_pending`. An
  unreachable router writes `{"reachable":false}` so outages are visible.
- **`syslog-YYYY-MM-DD.log`** — the router's syslog, mirrored **line-exact and rotation-aware**
  (line-count offset keyed on the file inode: no gaps, no duplicates across the daily
  `syslog.log → syslog.log-1` rotation).
- **`events-YYYY-MM-DD.jsonl`** — edge events only: `reboot`, `wan_ip_change`,
  `firmware_state_change`, `router_unreachable`/`router_reachable`.

## How it reaches the router

It SSHes out **reusing the router-hands key** — the same pinned `secrets/router_id_ed25519` +
`known_hosts` that the [router-hands](../router-hands/) relay uses — mounted **read-only**. Nothing
new to provision; the router host key is already pinned. A **single multiplexed connection**
(`ControlMaster`/`ControlPersist=300`) is reused across polls so this doesn't spam the router's own
auth log (≈1 login / 5 min, not one per poll). The logger issues read-only commands only
(`nvram get`, `/proc`, `cat /jffs/syslog.log`); it never mutates the router.

> Reusing the admin key means an always-on container now also holds it. To isolate later, generate
> a dedicated keypair, add its pubkey to the router's `authorized_keys`, and point `ROUTER_KEY` at
> it — revoking the logger then doesn't touch router-hands.

## Deploy (on kappa)

Prereqs:
- the router-hands secrets must already exist (they do, since router1 is deployed):
  `/volume1/docker/mage-hands/router-hands/secrets/{router_id_ed25519 (chmod 600), known_hosts}`;
- a gitignored `.env` in this dir setting the **real** router SSH user (the committed default is the
  `admin` placeholder), matching router-hands' `ROUTER_USER`:
  ```sh
  printf 'ROUTER_USER=%s\n' "$(grep -E '^ROUTER_USER=' ../router-hands/.env | cut -d= -f2)" > .env
  chmod 600 .env
  ```

```sh
sudo docker compose -f /volume1/docker/mage-hands/router-monitor/compose.yaml up -d --build
```

Read it:

```sh
tail -F /volume1/docker/mage-hands/router-monitor/data/health-$(date -u +%F).jsonl
tail -F /volume1/docker/mage-hands/router-monitor/data/syslog-$(date -u +%F).log
sudo docker exec router-monitor sh /app/summary.sh
```

Edits to `monitor.sh`/`summary.sh` are bind-mounted — `docker restart router-monitor` to apply (no
rebuild). Config is via `environment:` in `compose.yaml`.

## Config (env)

| var | default | meaning |
|---|---|---|
| `HEALTH_EVERY` | `60` | health snapshot cadence (s) |
| `SYSLOG_EVERY` | `120` | syslog mirror cadence (s); `0` disables the mirror |
| `RETAIN_DAYS` | `365` | prune per-day logs older than this |
| `ROUTER_HOST`/`ROUTER_USER`/`ROUTER_PORT` | `192.168.1.1`/`admin`/`22` | SSH target |
| `ROUTER_KEY`/`KNOWN_HOSTS` | `/secrets/router_key`/`/secrets/known_hosts` | in-container paths |
| `SYSLOG_PATH` | `/jffs/syslog.log` | router-side syslog to mirror |
| `ALERT_NTFY_URL`/`ALERT_WEBHOOK_URL` | empty | edge alerts (off unless set); ntfy = POST body, webhook = `{"text":…}` |

## Notes / gotchas

- **Broadcom rogue `sh`**: the remote health probe uses only `nvram`/absolute `/proc` paths and no
  `awk`/`sed`, and never invokes a bare `sh -c`, so it sidesteps the memory-tool `sh` shadowing that
  bit router-hands (see [../lessons.md](../lessons.md)).
- **`temp_c` is often null** on this Broadcom SoC (no standard `thermal_zone`); that's expected, not
  a failure.
- This is **telemetry, not control** — it has no `reboot`/mutation path. Operating the router stays
  with the router-hands relay.
