# router-hands

MCP relay for administering an **ASUS router running Asuswrt-Merlin**, reusing `mage_hands_core`.

Unlike `synology-hands` (a privileged container driving its *own* host via `nsenter`), a consumer
Merlin router has **no Docker** and a constrained BusyBox userland, so the relay can't run on it.
Instead the relay runs in an **unprivileged container on a NAS** (`kappa`) and reaches the router
**over SSH** (`SSHRunner`). The router is left stock except: enable SSH + add one public key.

Because `kappa`'s `:443` is already used by `synology-hands`, router-hands gets its **own tailnet
node** via a **Tailscale sidecar container**, reachable at `https://router1.<tailnet>.ts.net/mcp`.

```
Mac (Claude) ──https──> router1.<tailnet>.ts.net:443        [container stack on kappa]
                          │  tailscale sidecar (node "router1", userspace)
                          │   declarative serve (serve.json) → 127.0.0.1:8788
                          ▼  (shared netns: network_mode service:tailscale)
                         relay container (UNprivileged) FastMCP /mcp
                          │   token auth + audit + run()-gate + read policy  (from common/)
                          ▼  ssh -i /secrets/router_key admin@<router>  (egress over the LAN)
                         ASUS Merlin router (BusyBox + dropbear) — runs commands as root
```

## Trust model (read this)

Two guarantees are weaker here than on the NAS relays — by design, and the controls are placed
accordingly:

- **`read_file` over SSH is best-effort constrained reading on a *trusted* appliance, not
  filesystem confinement.** `PathPolicy` is lexical and can't resolve *remote* symlinks, so the
  explicit `READ_DENY` list (dropbear keys, VPN/cert material, `/etc/shadow`, world-writable
  `/var/tmp`·`/tmp/var`) is the real boundary. Keep ALLOW roots conservative.
- **`run()` is ON by default** (parity with `synology-hands`; set `ROUTER_ENABLE_RUN=false` to turn
  it off). This is the appliance's most security-sensitive surface: with run() registered, **token
  possession effectively grants constrained root shell on the router host by default** — a real blast
  radius on a soft-brickable target. It stays behind the dry-run/exec_token gate + a router-tuned
  denylist (blocks firmware flash, `nvram erase`, factory reset, mtd writes, and the core/router
  reboot backstops — see below). `ROUTER_ENABLE_RUN=false` is the escape hatch for environments where
  that posture is unacceptable. The denylist is a *lexical guardrail, not a sandbox* — real safety is
  the gate + audit + ephemerality + human approval.
- **`reboot_router` is the only directly-intended reboot path.** Because run() is now on by default,
  the router denylist explicitly closes the indirect Merlin reboot triggers that slip the core
  command-position anchor (`service reboot`, `init 6`/`telinit 6`, `busybox reboot`, `rc reboot`,
  `killall rc`). String-wrapped forms (`sh -c reboot`, `echo reboot | sh`) remain evadable — that's
  the lexical-backstop limitation, not a containment guarantee.

## Tools

| Tool | Tier | What it does |
|------|------|--------------|
| `system_info` | A (read) | kernel (`uname -a`), Merlin firmware (build/version), uptime, load, memory |
| `diagnostics` | A | SSH transport self-test: reachability, latency, `transport_error`, PATH sanity |
| `clients` | A | connected clients + WiFi associations (`/tmp/clientlist.json`, ARP, `wl assoclist`) |
| `dhcp_leases` | A | active dnsmasq leases |
| `wan_status` | A | WAN0/WAN1 state/IP/gateway/proto/DNS (dual-WAN aware) |
| `interfaces` | A | `ifconfig` / `ip -s addr` / `/proc/net/dev` counters |
| `firewall_show` | A | iptables filter + NAT tables (read-only) |
| `disk_usage` | A | `df -h` (jffs/tmpfs/USB) + jffs inode pressure + mount table |
| `performance` | A | cpu/load/mem+swap, instantaneous iowait, top procs, thermal (CPU + per-radio WiFi), conntrack pressure |
| `pending_updates` | A | Merlin firmware update state (`nvram webs_state_*`) + current fw + AiProtection sigs; `check=true` triggers a live ASUS check |
| `internet_exposure` | A | full WAN attack-surface: remote admin, SSH/telnet, port forwards, UPnP (+active), DMZ, DDNS, AiCloud, VPN servers, IPv6 fw, FTP/Samba, port-trigger, live listeners |
| `read_file` | A | policied file read over SSH (allow/deny roots) |
| `restart_service` | B (mutation) | Merlin `service restart_<name>` for an allowlisted set |
| `reboot_router` | B (mutation) | reboot the router — approval- **and** `confirm=true`-gated; SSH drop is expected |
| `run` | C (gated, **on by default**) | arbitrary root over SSH — present unless `ROUTER_ENABLE_RUN=false` |

## Prerequisites

- A NAS host that runs Docker and is on your tailnet (we use **kappa**), with this repo synced to
  `/volume1/docker/mage-hands` (the image build context is the **repo root**).
- A Tailscale **auth key** (reusable + ephemeral + tag, e.g. `tag:relay`) for the `router1` node,
  and MagicDNS + HTTPS certs enabled on the tailnet.
- **SSH enabled on the router** (Asuswrt-Merlin: Administration → System → Enable SSH) and the
  relay's public key in its authorized keys. The router needs nothing else.

## Deploy (on/from the Mac → kappa)

1. **Generate the router SSH key on the Mac** (keeps the private key off the router's shell history):
   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/router_id_ed25519 -C "router-hands@kappa" -N ""
   ```
   Add `~/.ssh/router_id_ed25519.pub` to the router (WebUI → Administration → System → "SSH
   Authorized Keys"). Confirm it works: `ssh -i ~/.ssh/router_id_ed25519 admin@<router> true`.

2. **Pre-create the runtime dirs on kappa** (gitignored; not in the repo) and place the secrets:
   ```sh
   ssh <admin>@kappa.local '
     B=/volume1/docker/mage-hands/router-hands
     sudo install -d -m 700 -o root -g root "$B/logs"
     install -d "$B/ts-state" "$B/secrets"'
   scp ~/.ssh/router_id_ed25519 <admin>@kappa.local:/volume1/docker/mage-hands/router-hands/secrets/
   ssh <admin>@kappa.local 'chmod 600 /volume1/docker/mage-hands/router-hands/secrets/router_id_ed25519'
   # Pin the router host key (verify the fingerprint against the router WebUI before trusting it):
   ssh <admin>@kappa.local 'ssh-keyscan -p 22 <router> > /volume1/docker/mage-hands/router-hands/secrets/known_hosts'
   ```

3. **Write `.env` on kappa** (`cp .env.example .env`, chmod 600). Fill `RELAY_TOKEN`
   (`openssl rand -hex 32`, also saved to `~/.config/nas-relay/router1.token` on the Mac),
   `TS_AUTHKEY`, `ROUTER_HOST`/`ROUTER_USER`, and keep `ALLOWED_USERS` **empty** for first bring-up.
   `BIND_HOST=127.0.0.1` and `PORT=8788` are required (the relay binds loopback inside the sidecar
   netns). On the very first connect you may set `ROUTER_STRICT_HOST_KEY=accept-new`, then flip back
   to `yes` (the relay warns loudly if you leave `accept-new` on with a populated `known_hosts`).

4. **Bring it up** (builds the relay, pulls the pinned Tailscale image, starts both):
   ```sh
   ssh <admin>@kappa.local 'sudo sh /volume1/docker/mage-hands/router-hands/scripts/relay-up.sh'
   ```
   It waits for the relay to be healthy, prints the sidecar's tailnet status, and runs a
   **SSH-egress PASS/FAIL** check (proves the relay can reach the router — see the egress note
   below).

5. **Smoke-test from inside the container** (8788 is on the sidecar's netns loopback, not kappa's):
   ```sh
   ssh <admin>@kappa.local '
     cd /volume1/docker/mage-hands/router-hands
     TOK=$(grep ^RELAY_TOKEN= .env | cut -d= -f2-)
     sudo docker exec -i -e RELAY_TOKEN="$TOK" router-hands python - < scripts/smoke-test.py'
   ```

6. **Connect from the Mac**, make one call, learn your identity, then lock the allowlist:
   ```sh
   claude mcp add --transport http --scope user router1 \
     https://router1.<tailnet>.ts.net/mcp \
     --header "Authorization: Bearer $(cat ~/.config/nas-relay/router1.token)"
   # ask Claude for `system_info`, then:
   ssh <admin>@kappa.local 'sudo tail -1 /volume1/docker/mage-hands/router-hands/logs/audit.jsonl'  # -> "user": ...
   ```
   Set `ALLOWED_USERS` to that Tailscale login in `.env` and `sudo docker compose up -d --force-recreate`.

7. **Tailscale ACL:** tag `router1` and grant only your identity `tcp:443` (same shape as the NAS).

8. **Idle watchdog:** add a DSM Task Scheduler root job (every 5 min) running
   `…/router-hands/scripts/idle-watchdog.sh` — stops the stack after 30 min idle.

9. **Scoped passwordless sudo:** `sudo sh …/router-hands/scripts/install-sudo.sh` (installs
   `/usr/local/sbin/mage-hands-router-relay-{up,down}` + `/etc/sudoers.d/mage-hands-router`,
   distinct from synology's so they coexist).

10. **Mac wiring:** add a `router1` case to `~/.config/mage-hands/relay.sh` whose host is
    **kappa** (the container host, not the router) calling `mage-hands-router-relay-{up,down}`, and
    add `mcp__router1__*` rules to `~/.claude/settings.json` (read-only tools in `allow`;
    `restart_service`/`reboot_router`/`run` + the `relay.sh` helper in `ask`). See
    [docs/deploy.md](../docs/deploy.md).

## SSH egress note (the one thing to verify)

The sidecar runs Tailscale in **userspace** mode (`TS_USERSPACE=true`) so the stack stays
unprivileged. Tailnet traffic uses the userspace netstack; SSH to the router's **LAN** IP egresses
via the container's Docker bridge — which works as long as kappa can reach the router (it's on the
same LAN). `relay-up.sh` prints `SSH egress to router: PASS/FAIL`. If it ever FAILs, switch the
sidecar to kernel-TUN mode: set `TS_USERSPACE=false` and add to the `tailscale` service
`devices: ["/dev/net/tun"]` + `cap_add: [NET_ADMIN, NET_RAW]`.

## Teardown

```sh
ssh <admin>@kappa.local 'sudo /usr/local/sbin/mage-hands-router-relay-down'   # or relay.sh router1 down
```
Stops the relay **and** the sidecar, so serve and the `router1` node disappear with it.

## How this differs from synology-hands

| | synology-hands | router-hands |
|--|----------------|--------------|
| Runner | `NsenterRunner` (own host) | `SSHRunner` (remote router) |
| Container | `privileged` + `pid:host` + `/:/host` | unprivileged, no host mount |
| Ingress | host's `tailscale serve` CLI on `:443` | Tailscale **sidecar** node + declarative `serve.json` |
| `read_file` | `fs_reader("/host")` (real fs guard) | `runner_reader` over SSH (lexical policy only) |
| `run()` | enabled | **on by default** (`ROUTER_ENABLE_RUN=false` to disable) |
| Port | `8787` | `8788` (loopback in the shared netns) |

See [ARCHITECTURE.md](../ARCHITECTURE.md) for the second deployment shape and
[docs/maintenance.md](../docs/maintenance.md) for sidecar/auth-key/SSH-key rotation.
