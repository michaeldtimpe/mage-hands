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
