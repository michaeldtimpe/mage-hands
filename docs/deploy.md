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

A small helper makes start/stop a single command that *also* enables full tool functionality:
on `up` it pre-authorizes **all** of the appliance's MCP tools in `~/.claude/settings.json`
(`mcp__<appliance>` matches every tool from that server, including ones added later) and then
starts the relay. The permission change takes effect on the **next** session (MCP tools and
settings both load at session start), which is exactly when the relay's tools appear.

```sh
cat > ~/.config/mage-hands/relay.sh <<'EOF'
#!/bin/sh
# Start/stop a relay; on `up` also pre-authorize ALL of the appliance's MCP tools in
# ~/.claude/settings.json so a fresh session has full functionality with no per-call prompts.
# Add a case per appliance.
APP="$1"; ACT="$2"
case "$APP" in
  <name>) HOST=<admin>@<nas>.local ;;
  *) echo "unknown appliance: $APP" >&2; exit 2 ;;
esac
enable_all_tools() {
  python3 - "$HOME/.claude/settings.json" "mcp__$APP" <<'PY'
import json, os, sys
path, rule = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: data = json.load(f)
except FileNotFoundError:
    data = {}
except (OSError, ValueError) as e:
    print(f"relay.sh: cannot update {path} ({e})", file=sys.stderr); sys.exit(0)
allow = data.setdefault("permissions", {}).setdefault("allow", [])
if rule not in allow:
    allow.append(rule)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f: json.dump(data, f, indent=2); f.write("\n")
    os.replace(tmp, path)
    print(f"relay.sh: enabled all '{rule}' tools (effective next session)")
PY
}
case "$ACT" in
  up)   enable_all_tools
        ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o LogLevel=ERROR "$HOST" "sudo -n /usr/local/sbin/mage-hands-relay-up" ;;
  down) ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o LogLevel=ERROR "$HOST" "sudo -n /usr/local/sbin/mage-hands-relay-down" ;;
  *) echo "usage: relay.sh <appliance> up|down" >&2; exit 2 ;;
esac
EOF
chmod +x ~/.config/mage-hands/relay.sh
```

Optional but handy — thin shell shortcuts so you can type `start-kappa-relay` / `start-relay`
instead of the full path. Drop them in `relay-aliases.sh` and source it from your shell rc:

```sh
cat > ~/.config/mage-hands/relay-aliases.sh <<'EOF'
# mage-hands relay control shortcuts — each wraps: relay.sh <appliance> up|down
start-kappa-relay()  { ~/.config/mage-hands/relay.sh kappa  up; }
stop-kappa-relay()   { ~/.config/mage-hands/relay.sh kappa  down; }
start-alpha-relay()  { ~/.config/mage-hands/relay.sh alpha  up; }
stop-alpha-relay()   { ~/.config/mage-hands/relay.sh alpha  down; }
start-all-relays()   { start-kappa-relay && start-alpha-relay; }
stop-all-relays()    { stop-kappa-relay;   stop-alpha-relay; }
start-relay()        { start-all-relays; }   # bring up BOTH NAS relays at once
stop-relay()         { stop-all-relays; }    # bring both down
EOF
echo '[ -f "$HOME/.config/mage-hands/relay-aliases.sh" ] && source "$HOME/.config/mage-hands/relay-aliases.sh"' >> ~/.zshrc
```

The only permission rule you must add by hand is the one that lets `relay.sh` run unprompted (it's
the script that then grants the rest):

```jsonc
{ "permissions": {
    "allow": [ "Bash(/Users/<you>/.config/mage-hands/relay.sh:*)" ],
    "ask":   [] } }
```

`relay.sh <appliance> up` appends `mcp__<appliance>` to `permissions.allow`, so **every** tool from
that appliance — read-only audit/diagnose *and* mutation (`restart_*`, `firewall_*`, gated `run`) —
runs without per-call prompts. The relay's own server-side controls still apply: the catastrophic
denylist, the two-call `exec_token` gate on `run()`, the identity allowlist, and the audit log. If
you'd rather keep mutation gated with an approval prompt, omit the `mcp__<appliance>` shortcut and
instead enumerate only the read-only tools in `allow` (leaving `restart_*`/`firewall_enable`/… out
so they prompt).

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

## Variant: SSHRunner relay with a Tailscale sidecar (router-hands)

The runbook above assumes the relay runs *on* the target. For a target that can't host the relay
(an ASUS Asuswrt-Merlin router: no Docker, no nsenter), the relay runs in a container on a NAS
(`kappa`) and reaches the router **over SSH**. The full step-by-step lives in
[../router-hands/README.md](../router-hands/README.md); the deltas from the NAS runbook are:

- **No privileged/pid:host/`/:/host`.** The relay only SSHes out. The SSH **private key** is
  bind-mounted read-only (`router-hands/secrets/router_key`, chmod 600, gitignored) — never baked
  into the image; the router host key is **pinned** (`secrets/known_hosts` via `ssh-keyscan`, verified
  out-of-band). The router is otherwise stock: enable SSH + add the public key.
- **Ingress is a Tailscale sidecar, not the host's serve CLI.** A `tailscale/tailscale` container
  (`hostname: router1`, `TS_USERSPACE=true`, `TS_SERVE_CONFIG=serve.json`) gives router-hands its own
  node `router1.<tailnet>.ts.net`; the relay uses `network_mode: "service:tailscale"`. So you set
  `BIND_HOST=127.0.0.1` + `PORT=8788` in `.env`, and there is **no** `tailscale serve` command.
  Pre-create `router-hands/{logs,ts-state,secrets}` on kappa (logs root-owned `700`).
- **Auth key.** Create a reusable + ephemeral + tagged `TS_AUTHKEY` for `router1` (Settings → Keys).
- **Smoke test runs inside the container** (`docker exec … router-hands python - < scripts/smoke-test.py`),
  because `8788` lives on the sidecar's netns loopback, not kappa's host loopback.
- **Bring-up verifies SSH egress.** `relay-up.sh` prints `SSH egress to router: PASS/FAIL`; if it
  FAILs, the userspace-netns LAN-egress fallback (kernel-TUN) is in router-hands/README.md.
- **`run()` is ON by default** (parity with synology-hands; `ROUTER_ENABLE_RUN=false` for
  inspection-only). The router denylist also closes indirect Merlin reboot paths so the gated
  `reboot_router` stays the only intended reboot route.
- **Scoped sudo uses distinct names** (`mage-hands-router-relay-{up,down}` + `/etc/sudoers.d/mage-hands-router`)
  so it coexists with synology-hands on the same box; the Mac `relay.sh router1` case SSHes to
  **kappa** (the container host), not the router.
