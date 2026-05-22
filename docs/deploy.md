# Deploy & operate an appliance

End-to-end runbook for deploying a relay to a new appliance (using a Synology NAS as the
worked example) and operating it. To *use* an already-deployed relay, see
[getting-started.md](getting-started.md).

## Prerequisites (per appliance)

- **x86 Synology**, DSM 7.2+, **Container Manager** installed.
- **Tailscale** (official Package Center app) joined to your tailnet. On DSM 7, add a boot-up
  Task Scheduler root task for a real TUN device:
  `/var/packages/Tailscale/target/bin/tailscale configure-host && synosystemctl restart pkgctl-Tailscale.service`
- In the Tailscale admin console: **MagicDNS** on and **HTTPS certificates** enabled (Serve
  needs this; first `serve` run otherwise prints a consent URL to click).
- **SSH** enabled (Control Panel → Terminal & SNMP). Deploy is via SSH because the Container
  Manager GUI cannot set `privileged`.
- An admin user on the NAS for deployment.

## 1. Establish SSH (key-based)

```sh
ssh-copy-id -i ~/.ssh/id_ed25519.pub <admin>@<nas>.local
# Synology gotcha: fix home perms or key auth silently fails
ssh <admin>@<nas>.local 'chmod 700 ~ ~/.ssh && chmod 600 ~/.ssh/authorized_keys'
ssh -o BatchMode=yes <admin>@<nas>.local 'echo KEY_OK'    # must succeed without a password
```

## 2. Recon (confirm the box-specific bits)

```sh
ssh <admin>@<nas>.local 'uname -m; cat /etc/VERSION'                     # x86_64, DSM 7.2.x
ssh <admin>@<nas>.local 'command -v docker || echo /usr/local/bin/docker'  # CLI path
sudo /var/packages/Tailscale/target/bin/tailscale status --json | grep -i '"DNSName"'  # MagicDNS name
```
Note the `docker` path (Synology's `sudo` `secure_path` excludes `/usr/local/bin`; the scripts
already resolve full paths) and the appliance's `<name>.<tailnet>.ts.net` MagicDNS name.

## 3. Copy the repo to the appliance

The image build context is the **repo root** (it `COPY`s `common/`), so sync the whole repo.
Create the target owned by your admin user first so you can `rsync` without root:

```sh
ssh <admin>@<nas>.local 'sudo mkdir -p /volume1/docker/mage-hands && sudo chown -R <admin>:users /volume1/docker/mage-hands'
rsync -az --exclude='.git/' --exclude='**/__pycache__/' --exclude='.env' --exclude='**/logs/' \
  -e 'ssh -i ~/.ssh/id_ed25519' \
  ~/Downloads/mage-hands/ <admin>@<nas>.local:/volume1/docker/mage-hands/
```

## 4. Configure secrets

```sh
# On the Mac: per-appliance token (chmod 600)
mkdir -p ~/.config/nas-relay && openssl rand -hex 32 > ~/.config/nas-relay/<name>.token
chmod 600 ~/.config/nas-relay/<name>.token
```
Write the appliance `.env` (same token value, `chmod 600`, never committed). Start with
`ALLOWED_USERS` **empty** — you'll set it after confirming your identity in step 7:

```ini
RELAY_TOKEN=<the token>
NODE_ID=<name>
ALLOWED_USERS=
```

## 5. Create the logs dir and build

Synology won't auto-create bind-mount sources, so make `logs/` first (root-owned, `700`, so
other DSM users can't read the command history):

```sh
ssh <admin>@<nas>.local '
  sudo install -d -m 700 -o root -g root /volume1/docker/mage-hands/synology-hands/logs
  cd /volume1/docker/mage-hands/synology-hands && sudo /usr/local/bin/docker compose up -d --build'
```

## 6. Verify over loopback (before exposing it)

```sh
ssh <admin>@<nas>.local '
  cd /volume1/docker/mage-hands/synology-hands
  TOK=$(grep ^RELAY_TOKEN= .env | cut -d= -f2-)
  sudo /usr/local/bin/docker exec -i -e RELAY_TOKEN="$TOK" synology-hands python - < scripts/smoke-test.py'
```
Expect: tools listed with the valid token, and the bad token rejected (401).

## 7. Expose via Tailscale Serve, then connect from the Mac

```sh
ssh <admin>@<nas>.local 'sudo /var/packages/Tailscale/target/bin/tailscale serve --bg --https=443 http://localhost:8787'

# From the Mac:
claude mcp add --transport http --scope user <name> \
  https://<name>.<tailnet>.ts.net/mcp \
  --header "Authorization: Bearer $(cat ~/.config/nas-relay/<name>.token)"
claude mcp list      # <name> -> ✓ Connected
```

Make one tool call (e.g. ask Claude for `system_info`), then read the audit log to learn your
**Tailscale identity**:

```sh
ssh <admin>@<nas>.local 'sudo tail -1 /volume1/docker/mage-hands/synology-hands/logs/audit.jsonl'
# -> "user": "you@example.com"
```

## 8. Lock the identity allowlist

Set `ALLOWED_USERS` to the identity you just confirmed and recreate:

```sh
ssh <admin>@<nas>.local '
  cd /volume1/docker/mage-hands/synology-hands
  TOK=$(grep ^RELAY_TOKEN= .env | cut -d= -f2-)
  printf "RELAY_TOKEN=%s\nNODE_ID=<name>\nALLOWED_USERS=you@example.com\n" "$TOK" > .env && chmod 600 .env
  sudo /usr/local/bin/docker compose up -d --force-recreate'
```

## 9. Restrict access at the network layer (Tailscale ACL)

In the admin console (Access controls), tag the appliance and grant only your identity:

```jsonc
{ "tagOwners": { "tag:relay": ["you@example.com"] },
  "grants": [ { "src": ["you@example.com"], "dst": ["tag:relay"], "ip": ["tcp:443"] } ] }
```
Tag the NAS at login: `tailscale up --advertise-tags=tag:relay`.

## 10. Schedule the idle auto-stop

`/etc/crontab` is DSM-managed (regenerated from Task Scheduler), so add the watchdog via the GUI:

**Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**
- **General:** User = `root`, name = `mage-hands idle watchdog`
- **Schedule:** Daily, "repeat every 5 minutes"
- **Run command:** `/volume1/docker/mage-hands/synology-hands/scripts/idle-watchdog.sh`

Stops the relay after 30 minutes idle (override with `IDLE_SECONDS`). It reads `last_activity`
(touched on every tool call) and runs `relay-down.sh` when stale.

## 11. Scoped passwordless sudo (recommended)

So Claude (or you) can start/stop the relay without the admin password — while keeping
everything else password-gated — run the installer **as root, once**:

```sh
ssh <admin>@<nas>.local 'sudo sh /volume1/docker/mage-hands/synology-hands/scripts/install-sudo.sh'
```

It installs **root-owned copies** of the lifecycle scripts to `/usr/local/sbin/mage-hands-relay-{up,down}`
(the relay user can't edit them, and `/usr/local`'s root-owned parent stops it swapping the
directory), then writes `/etc/sudoers.d/mage-hands` granting NOPASSWD for exactly those two
paths. Verify the scope holds:

```sh
ssh <admin>@<nas>.local 'sudo -n /usr/local/sbin/mage-hands-relay-down'   # works, no password
ssh <admin>@<nas>.local 'sudo -n id'                                      # MUST fail: "a password is required"
```

The second command failing is the point: only the relay lifecycle is passwordless; anything
genuinely destructive still requires the password (= a human). Re-run `install-sudo.sh` after
editing `relay-up.sh` / `relay-down.sh` to refresh the copies.

## 12. Claude Code permissions (on the Mac)

A small helper makes start/stop a single, gateable command:

```sh
cat > ~/.config/mage-hands/relay.sh <<'EOF'
#!/bin/sh
# Usage: relay.sh <appliance> up|down  — add a case per appliance.
APP="$1"; ACT="$2"
case "$APP" in
  <name>) HOST=<admin>@<nas>.local ;;
  *) echo "unknown appliance: $APP" >&2; exit 2 ;;
esac
case "$ACT" in
  up|down) ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o LogLevel=ERROR \
             "$HOST" "sudo -n /usr/local/sbin/mage-hands-relay-$ACT" ;;
  *) echo "usage: relay.sh <appliance> up|down" >&2; exit 2 ;;
esac
EOF
chmod +x ~/.config/mage-hands/relay.sh
```

Then add permission rules to `~/.claude/settings.json` so read-only tools run freely while
mutation, raw exec, and starting the relay require approval:

```jsonc
{ "permissions": {
    "allow": [ "mcp__<name>__system_info", "mcp__<name>__disk_usage",
               "mcp__<name>__storage_health", "mcp__<name>__list_containers",
               "mcp__<name>__container_logs", "mcp__<name>__service_status",
               "mcp__<name>__read_file" ],
    "ask":   [ "mcp__<name>__restart_container", "mcp__<name>__restart_service",
               "mcp__<name>__run",
               "Bash(/Users/<you>/.config/mage-hands/relay.sh:*)" ] } }
```

## Daily operation

```sh
~/.config/mage-hands/relay.sh <appliance> up      # from the Mac: build → healthy → serve (passwordless)
~/.config/mage-hands/relay.sh <appliance> down    # serve off → compose down
```

Or directly on the NAS: `sudo /usr/local/sbin/mage-hands-relay-up` / `-down`.

## Updating the relay

```sh
rsync -az --exclude='.git/' --exclude='.env' --exclude='**/logs/' \
  -e 'ssh -i ~/.ssh/id_ed25519' ~/Downloads/mage-hands/ <admin>@<nas>.local:/volume1/docker/mage-hands/
# if relay-up.sh / relay-down.sh changed, refresh the root-owned copies:
ssh <admin>@<nas>.local 'sudo sh /volume1/docker/mage-hands/synology-hands/scripts/install-sudo.sh'
~/.config/mage-hands/relay.sh up   # rebuilds (cached) and re-serves
```

## Rotating the token

Regenerate, update the NAS `.env` + Mac token file, recreate the container, and re-run
`claude mcp add` (remove first with `claude mcp remove <name>`). Use a separate token per
appliance so a compromise doesn't cascade.

## Token-on-Mac hygiene

`claude mcp add` stores the token **literally** in `~/.claude.json`. Exclude that file from
Time Machine / iCloud, or switch the MCP entry to a `headersHelper` script that reads the token
file at call time.
