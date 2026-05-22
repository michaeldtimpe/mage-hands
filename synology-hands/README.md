# synology-hands

MCP relay for administering a Synology NAS (x86, DSM 7.2+). Runs as a **privileged,
ephemeral** container that drives the host via `nsenter -t 1`, exposed to your Mac over
Tailscale Serve (HTTPS, tailnet-private).

## Prerequisites (per NAS)

- x86 "+" model, DSM 7.2+, **Container Manager** installed.
- **Tailscale** (official Package Center app) joined to your tailnet. On DSM 7 add a boot-up
  Task Scheduler root task to get a real TUN:
  `/var/packages/Tailscale/target/bin/tailscale configure-host && synosystemctl restart pkgctl-Tailscale.service`
- In the Tailscale admin console: enable **MagicDNS** and **HTTPS certificates**.
- SSH enabled (Control Panel → Terminal & SNMP). Deploy is via SSH because Container
  Manager's GUI cannot set `privileged`.

## Deploy

```sh
# 1. Copy the repo to the NAS (whole repo — the image build needs ../common).
#    e.g. rsync -a ~/Downloads/mage-hands/ nas:/volume1/docker/mage-hands/
cd /volume1/docker/mage-hands/synology-hands

# 2. Configure secrets.
cp .env.example .env && chmod 600 .env
openssl rand -hex 32          # put this in RELAY_TOKEN (and reuse it on the Mac, below)

# 3. Bring it up (builds the image, waits for health, exposes via Tailscale Serve).
sh scripts/relay-up.sh

# 4. Lock down the audit log so other DSM users can't read Claude's command history.
sudo chmod 700 logs && sudo chown root:root logs
```

`MAGE_HANDS_DIR` overrides the deploy root (default `/volume1/docker/mage-hands`).

## Verify (Phase 1)

```sh
# Real MCP handshake + auth check (good token succeeds, bad token gets 401):
RELAY_URL=http://127.0.0.1:8787/mcp RELAY_TOKEN=<token> python scripts/smoke-test.py
```

## Connect from the Mac

```sh
mkdir -p ~/.config/nas-relay
printf '%s' '<the RELAY_TOKEN>' > ~/.config/nas-relay/nas1.token
chmod 600 ~/.config/nas-relay/nas1.token

claude mcp add --transport http --scope user nas1 \
  https://<nas1>.<tailnet>.ts.net/mcp \
  --header "Authorization: Bearer $(cat ~/.config/nas-relay/nas1.token)"

claude mcp list      # nas1 -> connected;  /mcp shows mcp__nas1__* tools
```

> The token is stored **literally** in `~/.claude.json`. Exclude that file from Time
> Machine / iCloud backups, or switch to a `headersHelper` script for rotation.

## Restrict to just your Mac (Tailscale ACL)

```jsonc
{ "tagOwners": { "tag:relay": ["you@example.com"] },
  "grants": [ { "src": ["you@example.com"], "dst": ["tag:relay"], "ip": ["tcp:443"] } ] }
// tag the NAS at login:  tailscale up --advertise-tags=tag:relay
```

## Idle auto-stop (default-on)

Install `scripts/idle-watchdog.sh` as a DSM **Task Scheduler** root job every 5 minutes. It
stops the relay (and `tailscale serve reset`) after 30 minutes idle (`IDLE_SECONDS` to tune).

## Teardown

```sh
sh scripts/relay-down.sh     # tailscale serve reset + docker compose down
```

## Tools

- **Tier A (inspection):** `system_info`, `disk_usage`, `storage_health`, `list_containers`,
  `container_logs`, `service_status`, `read_file` (allow/deny policied).
- **Tier B (controlled mutation):** `restart_container`, `restart_service`.
- **Tier C (raw exec):** `run(command, exec_token)` — dry-run first, replay the token to execute;
  catastrophic patterns refused outright. Edit `READ_ALLOW` / `READ_DENY` in `server.py` to tune
  the read policy per box.

## Notes / gotchas

- The container is privileged + `pid: host` + mounts `/:/host` → effectively root on the NAS
  while running. That's the design; keep it ephemeral.
- Host exec uses `nsenter`, so it uses the **host's** binaries (docker, smartctl, syno*). The
  container only ships `nsenter` (util-linux) + Python.
- Container env vars (incl. `RELAY_TOKEN`) do **not** leak into host process listings.
- `tailscale serve` config persists across reboots; if the NAS reboots mid-session the Serve
  listener stays up but the backend is gone — just `relay-up.sh` again.
