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

Claude can also bring the relay up itself — starting it is gated by an **approval prompt**
(see [Permissions](#permissions) below).

## 1. Bring the relay up

Remote MCP servers have zero attack surface when not running, so you start it for the session.
From the Mac (uses key-based SSH + the NAS's scoped passwordless sudo, so no password prompt):

```sh
~/.config/mage-hands/relay.sh kappa up      # or: alpha
```

This runs the root-owned `mage-hands-relay-up` on the NAS, which builds (cached after the first
time), waits for the container to report healthy, then exposes it via Tailscale Serve. It's done
when you see `synology-hands is up and served over Tailscale.`

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
> `diagnostics`, `clients`, `dhcp_leases`, `wan_status`, `interfaces`, `firewall_show`, `read_file`
> (auto-run), with `restart_service` gated and `run` only present when enabled. Its relay runs on
> kappa and reaches the router over SSH. Register once with
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
| read a config/log file (allowlisted paths) | `read_file` | A |
| restart a container or DSM service | `restart_container`, `restart_service` | B (mutation) |
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

A fresh session applies these rules from `~/.claude/settings.json` so routine work is
frictionless but anything with side effects pauses for you:

| Action | Behavior |
|--------|----------|
| Read-only tools (`system_info`, `disk_usage`, `storage_health`, `list_containers`, `container_logs`, `service_status`, `read_file`) | **auto-run**, no prompt |
| Mutation tools (`restart_container`, `restart_service`) | **approval prompt** each call |
| Raw exec (`run`) | **approval prompt** each call |
| Starting/stopping the relay (`relay.sh`) | **approval prompt** |

So Claude can investigate freely, but you approve every change. (On the NAS side, scoped
passwordless sudo only covers the relay lifecycle; any genuinely destructive sudo still needs
the password, which Claude doesn't have.)

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
| `kappa` shows disconnected in `/mcp` | Relay is down — run `relay-up.sh`. |
| Tools don't appear though `claude mcp list` says connected | Session started before the relay came up — start a new session. |
| `401` / "needs authentication" | Token mismatch — the Mac token (`~/.config/nas-relay/kappa.token`) must equal `RELAY_TOKEN` in the NAS `.env`. |
| Every tool call is rejected for identity | `ALLOWED_USERS` doesn't include your Tailscale login (`tailscale status`). |
| `relay-up.sh` hangs on Serve / asks for consent | Tailscale Serve/HTTPS not enabled on the tailnet — enable HTTPS in the admin console. |
