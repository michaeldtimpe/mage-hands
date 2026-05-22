# Maintenance & troubleshooting

Keeping the relays and their hosts healthy. For daily use see [getting-started.md](getting-started.md);
for first deploy see [deploy.md](deploy.md).

## Scheduled tasks on each box

Both are DSM **Task Scheduler** root jobs, created via the GUI (`/etc/crontab` is DSM-managed —
don't hand-edit). Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script,
User = `root`.

| Task | Schedule | Run command |
|------|----------|-------------|
| Idle relay auto-stop | every 5 min | `/volume1/docker/mage-hands/synology-hands/scripts/idle-watchdog.sh` |
| Tailscale auto-update | weekly | `/volume1/docker/mage-hands/synology-hands/scripts/tailscale-update.sh` |

## Updating Tailscale (Package Center lags — don't rely on it)

Synology's Package Center frequently stalls on the Tailscale package (it left kappa and alpha on
**1.58.2** for ~2 years). Use Tailscale's own self-updater instead — the bundled script sets the
required PATH for you:

```sh
sudo /volume1/docker/mage-hands/synology-hands/scripts/tailscale-update.sh
```

Equivalent raw command (note the PATH — `tailscale update` calls `synopkg`, which isn't on the
cron/nsenter PATH, so a bare invocation fails with exit 127):

```sh
sudo env PATH=/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin \
  /var/packages/Tailscale/target/bin/tailscale update --yes
```

Driving it through the relay (root via nsenter), background it so the daemon restart doesn't sever
the call mid-flight:

```
mcp__<name>__run  command="nohup sh -c 'sleep 5; PATH=/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin /var/packages/Tailscale/target/bin/tailscale update --yes > /tmp/ts-update.log 2>&1' >/dev/null 2>&1 &"
```
(dry-run first, then replay the `exec_token`; read `/tmp/ts-update.log` over LAN afterward).

## Checking for manually-updatable packages (Package Center lags)

Package Center silently lags upstream for some packages (it left Tailscale ~2 years behind). The
`pending_updates` Tier-A tool buckets the answer so you don't flatten "updates available":

- **DSM OS** — `synoupgrade --check` + current `productversion`. The check returns a status *token*
  (e.g. `UPGRADE_CHECKNEWDSM`), so confirm in Control Panel → Update & Restore. (Fleet note: kappa
  is **7.2.1** vs alpha **7.3.1** — a major upgrade is outstanding on kappa.)
- **Package Center** — `synopkg checkupdateall` (`[]` = nothing pending) + the installed list.
- **Vendor-managed** — Tailscale via `tailscale version` + `tailscale update --dry-run` (update with
  `tailscale update --yes`, **not** Package Center).
- **Container images** — compare with `list_containers` + `docker images`.

Ask Claude: *"Run `pending_updates` on kappa and tell me what I can update."*

## Checking external access / internet exposure

`internet_exposure` reports QuickConnect / DDNS / UPnP / port-forwarding / reverse-proxy, each with a
`confidence` (`authoritative` / `heuristic` / `unknown`) — so an unread probe is never mistaken for
"off". QuickConnect's truth lives in `/usr/syno/etc/synorelayd/synorelayd.conf` + the running
`synorelayd` daemon (**not** `/etc/synoinfo.conf`, the 2026-05 audit's mistake). To toggle it:

```sh
synowebapi --exec api=SYNO.Core.QuickConnect method=get                          # enabled? alias? services?
synowebapi --exec api=SYNO.Core.QuickConnect method=set version=1 enabled=false  # disable (synorelayd stops; server_id clears)
```

Auto-block lives in the `SYNO.Core.Security.AutoBlock` webapi (not a conf file). SSH password-auth
is in `/etc/ssh/sshd_config` (`PasswordAuthentication no`, then `synosystemctl reload-or-restart
sshd`) — note DSM may rewrite this file on a DSM update, so re-check after upgrades.

## Re-running the audit

The 2026-05 audit is **not** a one-shot artifact — re-run it against the *new* tools and correct
sources. Prefer `internet_exposure` / `performance` / `pending_updates` over ad-hoc `run()`, and
keep the detection lesson in mind (lessons.md, *"An empty probe is not a negative"*): a probe that
returns nothing means "off" **or** "wrong oracle". DSM 7 uses **`synosystemctl`** (not
`synoservicectl`) and keeps `syno*` tools in `/usr/syno/{bin,sbin}` — the relay's `NsenterRunner`
now puts those on `PATH` automatically.

## High host CPU? Suspect `tailscaled`, not the relay

The relay is a near-idle uvicorn process and is usually stopped by the watchdog anyway, so high
host CPU is almost certainly the host. We hit this on kappa: load ~5 on a 2-core box, with the old
1.58.2 `tailscaled` stuck at **364% CPU**.

```sh
# top consumers (ps/top truncate long paths; read /proc for the real process name)
top -b -n 1 | sed -n '8,16p'
# measure a specific tailscaled over 3s:
P=$(grep -l tailscaled /proc/[0-9]*/comm | head -1 | sed 's|/proc/||;s|/comm||')
hz=$(getconf CLK_TCK); a=$(awk '{print $14+$15}' /proc/$P/stat); sleep 3
b=$(awk '{print $14+$15}' /proc/$P/stat); echo "tailscaled $(( (b-a)*100/(hz*3) ))%"
```

Fix a stuck `tailscaled`: restart the package (immediate), then update (prevents recurrence):

```sh
sudo synopkg restart Tailscale
```

## Inspecting the relay

```sh
sudo docker inspect -f '{{.State.Health.Status}}' synology-hands   # healthy?
sudo docker logs --tail 50 synology-hands                          # server logs
pgrep -fc server.py                                                # 0 = relay stopped (idle)
sudo tail -5 /volume1/docker/mage-hands/synology-hands/logs/audit.jsonl   # who called what
```

## router-hands (SSHRunner relay + Tailscale sidecar, on kappa)

router-hands runs as a two-container stack on kappa (`router-hands` relay + `router-hands-ts`
sidecar) and reaches the ASUS Merlin router over SSH. Its idle-watchdog Task Scheduler job points
at `…/router-hands/scripts/idle-watchdog.sh`.

```sh
# Health / state
sudo docker inspect -f '{{.State.Health.Status}}' router-hands            # relay healthy?
sudo docker logs --tail 50 router-hands                                   # server logs (incl. run()-disabled / accept-new warnings)
sudo docker exec router-hands-ts tailscale status --peers=false           # sidecar joined the tailnet?
sudo tail -5 /volume1/docker/mage-hands/router-hands/logs/audit.jsonl     # who called what
# Prove the relay can still reach the router (the userspace-netns egress path):
sudo docker exec router-hands sh -c 'ssh -o BatchMode=yes -i /secrets/router_key -p "${ROUTER_PORT:-22}" "${ROUTER_USER:-admin}@${ROUTER_HOST}" true' && echo egress-OK
```

**Updating the sidecar Tailscale** — it's a pinned image (`tailscale/tailscale:v1.98.2` in
`compose.yaml`), not Package Center, so just bump the tag and recreate:

```sh
cd /volume1/docker/mage-hands/router-hands
sudo /usr/local/bin/docker compose pull tailscale && sudo /usr/local/bin/docker compose up -d
```

**Rotating `TS_AUTHKEY`** — the `router1` node identity persists in `./ts-state` once joined, so
day-to-day recreates don't re-auth. But auth keys expire: if the key expires *and* `ts-state` is
wiped, bring-up fails to join — issue a fresh reusable+ephemeral+tagged key, update `.env`, and
`compose up -d --force-recreate`.

**Rotating the router SSH key** — regenerate on the Mac, replace `router-hands/secrets/router_id_ed25519`
(chmod 600) on kappa, update the router's authorized keys, then recreate. If the **router's host
key** changes (e.g. firmware reflash), re-run `ssh-keyscan` into `secrets/known_hosts` or SSH will
refuse to connect (pinned host key mismatch).

**Sidecar-crash gap** — `depends_on` gates startup only. If `router-hands-ts` dies at runtime, the
relay keeps running on a broken netns until the idle watchdog tears the stack down; just
`relay.sh router1 down` then `up` to recover.
