# Maintenance & troubleshooting

Keeping the relays and their hosts healthy. For daily use see [getting-started.md](getting-started.md);
for first deploy see [deploy.md](deploy.md).

## Scheduled tasks on each box

All are DSM **Task Scheduler** root jobs (`/etc/crontab` is DSM-managed — don't hand-edit). The
idle-watchdog and Tailscale jobs were made in the GUI (Control Panel → Task Scheduler → Create →
Scheduled Task → User-defined script, User = `root`); the Plex and UPS jobs were created via the
**`synowebapi` recipe below** — there *is* a scriptable path, the GUI is not the only way.

| Task | Schedule | Box | Run command |
|------|----------|-----|-------------|
| Idle relay auto-stop | every 5 min | both | `/volume1/docker/mage-hands/synology-hands/scripts/idle-watchdog.sh` |
| Tailscale auto-update | weekly | both | `/volume1/docker/mage-hands/synology-hands/scripts/tailscale-update.sh` |
| Plex auto-update | weekly (Sun 04:00) | alpha (id 18) | `/volume1/docker/mage-hands/synology-hands/scripts/plex-update.sh` |
| UPS health check | daily (09:00) | alpha (id 19) | `/volume1/docker/mage-hands/synology-hands/scripts/ups-healthcheck.sh --quiet` |

### Creating Task Scheduler jobs via the webapi (no GUI)

`synoschedtask` is read-only, but `SYNO.Core.TaskScheduler method=create` works. The Plex + UPS
jobs above were made with (run as root, e.g. via `mcp__alpha__run`):

```sh
synowebapi --exec api=SYNO.Core.TaskScheduler method=create version=3 \
  name='mage-hands plex auto-update' owner=root type=script enable=true \
  schedule='{"date":"2026/5/22","date_type":0,"hour":4,"minute":0,"last_work_hour":4,"repeat_date":0,"repeat_hour":0,"repeat_min":0,"week_day":"0"}' \
  extra='{"notify_enable":false,"notify_if_error":false,"notify_mail":"","script":"/volume1/docker/mage-hands/synology-hands/scripts/plex-update.sh"}'
```

Schedule encoding (`date_type:0`, all `repeat_*:0` for a plain run-at-time-of-day):
- **daily** → `week_day:"0,1,2,3,4,5,6"`; **weekly** → one day `week_day:"0"` (0=Sun … 6=Sat);
  **every N min** → `repeat_min:N`. Time of day via `hour` / `minute`.
- **v3 gotcha:** omit `monthly_week` or it errors `4800 monthly_week not supported in v3`.
- **Email-on-failure** (the UPS down-alert): in `extra` set `notify_enable:true, notify_if_error:true,
  notify_mail:"<addr>"`. A non-zero script exit then emails once per run (DSM SMTP must be on).

Verify: `synoschedtask --get | grep -A4 '<name>'` or `synowebapi … method=get version=3 id=<id>`
(shows `Next Trigger`). To run now, use the GUI **Run** button — `method=run` queues but doesn't
reliably execute. The UPS job (id 19) emails `you@example.com` on a DOWN result.

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
  `tailscale update --yes`, **not** Package Center). **Plex** (a *package*, also lags Package Center)
  has its own updater — see "Updating Plex" below.
- **Container images** — compare with `list_containers` + `docker images`.

Ask Claude: *"Run `pending_updates` on kappa and tell me what I can update."*

## Updating Plex Media Server (Package Center lags — auto-update script)

Plex's package on Synology trails Plex's own releases (same problem as Tailscale), so
`scripts/plex-update.sh` updates it straight from Plex's download catalog. It reads
`https://plex.tv/api/downloads/5.json`, picks the build matching this box's **DSM major** (Plex
splits `Synology (DSM 7)` vs `Synology (DSM 7.2.2+)` — alpha on 7.3.1 uses the latter) and **CPU
arch**, compares to the installed version, then downloads + **sha1-verifies** + `synopkg install`s
the `.spk` and restarts Plex. It's a safe no-op on boxes without Plex and won't downgrade.

```sh
sudo /volume1/docker/mage-hands/synology-hands/scripts/plex-update.sh --check   # report only (exit 10 = update available)
sudo /volume1/docker/mage-hands/synology-hands/scripts/plex-update.sh           # update if newer
```

- **Channel:** stable/public by default. For the Plex Pass channel, set `PLEX_TOKEN=<your token>`.
- **Already scheduled** on alpha — weekly Sunday 04:00 (Task Scheduler id 18, created via the
  webapi recipe above). The `--check` form is handy for "is Plex behind?" without changing anything.

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

## UPS shows "unknown" / "not supported" / health broken

Don't trust the control-plane verdict — descend the layers (alpha 2026-05 case: a CyberPower whose
USB interface never enumerated). Diagnostic ladder:

```sh
synowebapi --exec api=SYNO.Core.ExternalDevice.UPS method=get     # DSM view: enable/mode/status/usb_ups_connect
cat /usr/syno/etc/ups/synoups.conf                                # persisted intent (ups_enabled/ups_mode/ups_acl)
for u in ups-usb upsd upsmon; do synosystemctl get-active-status $u; done   # daemons up?
grep -iE "ups|usbhid|not support" /var/log/messages | tail       # "This UPS is not supported. product=[]" = driver couldn't read it
grep -A2 '\[ups\]' /etc/ups/ups.conf                             # which NUT driver DSM chose (CyberPower wants usbhid-ups, not tripplite_usb)
timeout 12 /usr/bin/usbhid-ups -DD -a ups                         # raw driver debug (after temporarily setting driver=usbhid-ups)
cat /sys/bus/usb/devices/<b-p>/bNumInterfaces; ls -d /sys/bus/usb/devices/<b-p>:*   # ZERO interfaces => USB-link fault
```

`ups-usb.sh` auto-probes `usbhid-ups blazer_usb bcmxcp_usb richcomm_usb tripplite_usb` and writes the
first that returns a product; it writes `tripplite_usb` as a *give-up fallback*. If the raw driver
sees the VID:PID but fails `could not claim interface 0: No such file or directory`, and `/sys` shows
**0 interfaces**, the device enumerated but never exposed its HID interface — a **physical** problem
(re-seat/replace the USB cable, try another port, power-cycle the UPS). A software USB reset
(`echo -n <b-p> > /sys/bus/usb/drivers/usb/{unbind,bind}`) will *not* fix a non-enumerating
interface. Once the cable/port is fixed, DSM's auto-probe picks `usbhid-ups` itself.

## UPS health logging & daily down-alert

**What DSM logs already:** UPS *events* (`upsd`/`upsmon` start, `connected`, on-battery/low-battery/
lost-comms) go to `/var/log/messages` + **Log Center**, and `upsmon` is wired (`NOTIFYCMD upssched`,
`NOTIFYFLAG ONBATT/LOWBATT/NOCOMM EXEC`) to fire DSM notifications **on those events** — but **only
while upsmon is running.** Nothing flags the monitoring being *down* (daemon stopped / USB not
enumerating), which is the failure that hid the dead UPS in the 2026-05 audit.

**`scripts/ups-healthcheck.sh`** closes that gap. Read-only; run as root. It treats a UPS as
expected only where `synoups.conf ups_enabled=yes` (safe no-op elsewhere), then asks `upsc` whether
`upsd` is answering and reporting a status. Output, every run, lands in **`/var/log/mage-ups-health.log`**
(`$UPS_HEALTH_LOG` to override). On **DOWN** it raises visibility three ways: a `daemon.err` syslog
line (→ `/var/log/messages` + Log Center — note DSM's syslog keeps `err`/`warning` but drops
`notice`/`info`), a DSM **desktop notification** (`synodsmnotify`), and a **non-zero exit**.

```sh
sudo /volume1/docker/mage-hands/synology-hands/scripts/ups-healthcheck.sh          # OK -> exit 0, DOWN -> exit 1
sudo /volume1/docker/mage-hands/synology-hands/scripts/ups-healthcheck.sh --quiet  # speak up only when DOWN
tail /var/log/mage-ups-health.log                                                   # the health log
```

**Already scheduled** on alpha — daily 09:00 (Task Scheduler id 19, created via the webapi recipe
above with `notify_if_error:true`). Because a DOWN run exits non-zero, DSM **emails
`you@example.com` once a day while the UPS is down** (DSM SMTP is on: `smtp_mail_enabled=yes`)
— the "once-a-day if it's down" alert, riding DSM's own mail rather than fragile `synonotify` event
tags. (To recreate elsewhere or via the GUI, tick the task's **Settings** → *"Send run details by
email"* → *"only when the script terminates abnormally"*.)

## Checking SSD cache health, wear & effectiveness

The obvious tools fail: a Synology SSD cache is remapped to `/dev/nvc1`,`/dev/nvc2`, and on
boxes that take **M.2 SATA** SSDs (alpha runs 2× Intel D3-S4510 240GB on an M2D17 card) those
devices present as **SCSI**, so `smartctl -d nvme /dev/nvc1` dies with *"Read NVMe Identify
Controller failed … Inappropriate ioctl for device"* and there's no `nvme`/`synonvme` CLI on the
box. **DSM already polled the drives and cached the parsed result** — read that instead of
re-deriving it:

```sh
# DSM's verdict per cache device (authoritative): % life left, error state, temp, model
for d in nvc1 nvc2; do echo "== $d =="; for f in model serial remain_life smart read_only temperature; do
  echo -n "$f="; cat /run/synostorage/disks/$d/$f; done; done
# full SMART attribute table DSM parsed (JSON: [id, name, cur, worst, thresh, raw, failed])
cat /run/synostorage/disks/nvc1/smart_info_list.cache
```

`remain_life` is the number DSM shows in Storage Manager (100 = unworn; alpha read 100/99 on
2026-05-22). Cross-check the raw attributes: `Reallocated_Sector_Ct` / `Pending_Sector_Count` /
`Uncorrectable_Error_Cnt` / `CRC_Error_Count` should all be 0; `Media_Wearout_Indicator`
(normalized) counts 100→1; `Host_Writes_32MiB` × 32 MiB is lifetime host writes (datacenter SSDs
are rated for hundreds of TB, so a few TB written = ~1% wear even after years of power-on). Note
these were **used** drives — the two members' lifetime write counts differ by their pre-service
history; in-service wear is symmetric.

```sh
cat /proc/mdstat              # cache array: md4 = raid1 of nvc1p1/nvc2p1 => mirrored => READ-WRITE cache
dmsetup status               # cachedev_0 'flashcache-syno stats': read/write hit %, dirty %
```

**Reading effectiveness** (`dmsetup status`, alpha snapshot): `read hit percent(2)` —
near-useless, because ~96% of reads are sequential and DSM's cache **skips sequential I/O** by
design (a media/large-file volume gets almost nothing from read caching). `write hit percent(64)`
(dirty 62%) — the writeback half *is* absorbing random writes (metadata, Docker/DB I/O), which is
where the value is. A RAID1 mirror of **PLP** datacenter SSDs + the box's UPS is the correct, safe
setup for a writeback cache (a single-SSD or RAID0 cache can only be read-only). The cache:volume
ratio is tiny (~223 GiB front of ~37 TiB), fine for this workload.

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
