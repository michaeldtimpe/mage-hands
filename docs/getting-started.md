# Getting started — using a deployed relay

This is the everyday path: a relay is already deployed and you want Claude to use it from a
**fresh session**. (To deploy a new appliance, see [deploy.md](deploy.md).)

The relay is **off by default**, so there are two steps: bring it up, then start a new Claude
session so the tools load.

## TL;DR

```sh
# 1. Bring a relay up from the Mac (scoped passwordless sudo — no password).
#    Appliances: kappa, alpha (NAS tools); router1 (ASUS Merlin router tools — its relay runs on kappa).
~/.config/mage-hands/relay.sh kappa up

# 2. Start a NEW Claude Code session on your Mac.
#    Its tools auto-load as  mcp__kappa__*  (or mcp__alpha__*) — just ask Claude to use them.

# 3. When done (or let the idle watchdog do it after 30 min):
~/.config/mage-hands/relay.sh kappa down
```

Claude can also bring the relay up itself — it runs the same `relay.sh` helper, allowed without a
prompt via the `Bash(...relay.sh:*)` rule (see [Permissions](#permissions) below).

**Shortcuts** — `~/.config/mage-hands/relay-aliases.sh` (sourced by `~/.zshrc`) wraps the helper so
you can skip the path: `start-kappa-relay` / `start-alpha-relay` (and `stop-*`) for one box, and
**`start-relay`** to bring up *both* NAS relays at once (`relay.sh kappa up && relay.sh alpha up`);
`stop-relay` brings both down.

## 1. Bring the relay up

Remote MCP servers have zero attack surface when not running, so you start it for the session.
From the Mac (uses key-based SSH + the NAS's scoped passwordless sudo, so no password prompt):

```sh
~/.config/mage-hands/relay.sh kappa up      # or: alpha
```

This runs the root-owned `mage-hands-relay-up` on the NAS, which builds (cached after the first
time), waits for the container to report healthy, then exposes it via Tailscale Serve. It's done
when you see `synology-hands is up and served over Tailscale.`

`up` also **enables full tool functionality in one command**: it adds `mcp__<appliance>` to
`permissions.allow` in `~/.claude/settings.json`, so *every* tool from that appliance — read-only
audit/diagnose **and** mutation (`restart_*`, `firewall_*`, gated `run`) — runs without per-call
approval prompts in your next session. (The relay's server-side controls still apply: the
catastrophic denylist, the two-call `exec_token` gate on `run()`, the identity allowlist, and the
audit log.) Prefer per-call gating instead? See [Permissions](#permissions).

> **Claude starting it:** if you ask Claude to start the relay, it runs the same helper — and
> because that command is on the permissions `ask` list, you'll get an approval prompt first.

## 2. Start a fresh Claude session

Remote MCP servers are loaded **at session start**, so the relay's tools won't appear in a
session that was already open when you brought it up. Open a new Claude Code session and verify:

```sh
claude mcp list      # kappa: https://kappa.<tailnet>.ts.net/mcp (HTTP) - ✓ Connected
```

Inside the session, `/mcp` shows the server and its tools as `mcp__kappa__*`. If it shows
**disconnected**, the relay is down — go back to step 1.

> **First time on this Mac only:** if `kappa` isn't listed at all, register it once:
> ```sh
> claude mcp add --transport http --scope user kappa \
>   https://kappa.<tailnet>.ts.net/mcp \
>   --header "Authorization: Bearer $(cat ~/.config/nas-relay/kappa.token)"
> ```

> **router1** (the ASUS Merlin router) works the same way — `~/.config/mage-hands/relay.sh router1 up`,
> then a fresh session — but exposes **router** tools instead of NAS tools: `system_info`,
> `diagnostics`, `clients`, `dhcp_leases`, `wan_status`, `interfaces`, `firewall_show`, `disk_usage`,
> `performance`, `pending_updates`, `internet_exposure`, `read_file` (auto-run), with
> `restart_service` and `reboot_router` (approval+`confirm`-gated) gated, and `run` present by default
> (`ROUTER_ENABLE_RUN=false` to disable). Its relay runs on kappa and reaches the router over SSH.
> Register once with
> `claude mcp add --transport http --scope user router1 https://router1.<tailnet>.ts.net/mcp --header "Authorization: Bearer $(cat ~/.config/nas-relay/router1.token)"`.
> See [../router-hands/README.md](../router-hands/README.md).

## 3. What you can ask for

Just talk to Claude normally — it has these tools (all calls run on the NAS host and are
audited):

| Ask Claude to… | Tool | Tier |
|----------------|------|------|
| check kernel/DSM version | `system_info` | A (read) |
| show disk usage | `disk_usage` | A |
| check SMART health of every disk | `storage_health` | A |
| list containers / tail a container's logs | `list_containers`, `container_logs` | A |
| check a DSM service's status | `service_status` | A |
| check internet exposure (QuickConnect / DDNS / UPnP / port-forward / reverse-proxy) | `internet_exposure` | A |
| see resource pressure (load, memory, swap, iowait, top processes, temps) | `performance` | A |
| check for DSM / package / vendor (Tailscale) updates | `pending_updates` | A |
| audit the DSM firewall (enabled? enforced? active profile?) | `firewall_status` | A |
| list a firewall profile's rules (+ generated iptables) | `firewall_rules` | A |
| diagnose firewall issues (config↔runtime drift, admin reachability) | `firewall_diagnose` | A |
| read a config/log file (allowlisted paths) | `read_file` | A |
| restart a container or DSM service | `restart_container`, `restart_service` | B (mutation) |
| enable / disable / reload the DSM firewall | `firewall_enable`, `firewall_disable`, `firewall_reload` | B |
| edit a firewall profile's allow-list (lock-out-guarded) | `firewall_set_rules` | B |
| run an arbitrary root command | `run` | C (gated) |

Example prompts:
- *"Check the NAS disk usage and SMART health, and flag anything degraded."*
- *"Tail the last 100 lines of the `plex` container logs."*
- *"Restart the `radar-image-processor` container."*

## The `run()` tool (arbitrary root commands)

`run()` is the escape hatch for anything without a dedicated tool. It's a **two-step** flow so a
single call can't accidentally mutate the box:

1. Claude calls `run("…")` with no token → gets a **dry-run preview** plus a one-time
   `exec_token` (valid 5 minutes, bound to that exact command).
2. After you've seen the intended command, Claude calls `run("…", exec_token="…")` to execute.

Catastrophic commands (`rm -rf /`, `mkfs`, partition tools, wiping a volume, …) are **refused
outright**, before any token is issued. Targeted operations under a volume are allowed.

Good habit: ask Claude to **dry-run and show you the command first**, then confirm.

## Permissions

A fresh session applies the rules from `~/.claude/settings.json`. Because `relay.sh <appliance> up`
adds `mcp__<appliance>` to `permissions.allow`, **all** of that appliance's tools run without
per-call prompts once you start the next session:

| Action | Behavior |
|--------|----------|
| Every appliance tool — read-only (`firewall_status`, `firewall_diagnose`, `system_info`, …) **and** mutation (`restart_*`, `firewall_enable`/`firewall_set_rules`/…, gated `run`) | **auto-run** (granted by `mcp__<appliance>`) |
| Starting/stopping the relay (`relay.sh`) | runs unprompted via the `Bash(...relay.sh:*)` allow rule |

Frictionless by design — the safety lives on the relay, not in per-call prompts: the catastrophic
denylist, the two-call `exec_token` gate on `run()`, the Tailscale identity allowlist, the audit
log, and (on the NAS) scoped passwordless sudo that covers only the relay lifecycle — any genuinely
destructive sudo still needs the password, which Claude doesn't have. `firewall_set_rules` adds its
own lock-out guard.

**Want changes gated instead?** Don't use the `mcp__<appliance>` shortcut: remove it from `allow`
and enumerate only the read-only tools there, leaving mutation tools out so they prompt each call.
(If you do this, `relay.sh up` will re-add the shortcut — drop the `enable_all_tools` call from the
script too.)

## Bring it down

```sh
~/.config/mage-hands/relay.sh kappa down      # or: alpha
```

This turns off Tailscale Serve and stops the container. The `kappa` MCP server will then show
disconnected in `/mcp` — expected. If you forget, the **idle watchdog** stops it automatically
after 30 minutes of no tool calls (once scheduled — see [deploy.md](deploy.md)).

## Audit trail

Every tool call is logged on the NAS at
`/volume1/docker/mage-hands/synology-hands/logs/audit.jsonl` (root-readable only), one JSON line
per call with the caller identity, tool, args, status, and timing:

```sh
ssh magehands@kappa.local 'sudo tail -5 /volume1/docker/mage-hands/synology-hands/logs/audit.jsonl'
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `kappa` shows disconnected in `/mcp` | Relay is down — run `~/.config/mage-hands/relay.sh kappa up` (or the `start-relay` shortcut). |
| Tools don't appear though `claude mcp list` says connected | Session started before the relay came up — start a new session. |
| `401` / "needs authentication" | Token mismatch — the Mac token (`~/.config/nas-relay/kappa.token`) must equal `RELAY_TOKEN` in the NAS `.env`. |
| Every tool call is rejected for identity | `ALLOWED_USERS` doesn't include your Tailscale login (`tailscale status`). |
| `relay.sh … up` hangs on Serve / asks for consent | Tailscale Serve/HTTPS not enabled on the tailnet — enable HTTPS in the admin console. |
