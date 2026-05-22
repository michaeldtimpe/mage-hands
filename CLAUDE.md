# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repo.

## What this is
`mage-hands` is a set of **MCP relays** that let Claude administer home-lab appliances
remotely. Two layers:
- **Core** — the installable `mage_hands_core` package (`common/`): token auth, forensic
  audit, the gated `run()` tool, and the read path policy. All security logic lives here.
- **Appliances** — thin servers (`synology-hands/`, future `router-hands/`) that pick an
  executor and register target-specific tools.

## Start here
1. **[README.md](README.md)** — overview, how it works, repo layout, tool tiers.
2. **[ARCHITECTURE.md](ARCHITECTURE.md)** — request lifecycle, security model, tool tiers, audit schema, config reference.
3. **[AGENTS.md](AGENTS.md)** — contributor guide: key files, common tasks (add a tool, add an appliance, tune the denylist/read policy), deployed-appliance inventory.
4. **[lessons.md](lessons.md)** — deployment lessons + Synology gotchas (home perms, sudo PATH, `get_http_headers` stripping auth, `/etc/crontab` is DSM-managed, …).
5. **[docs/getting-started.md](docs/getting-started.md)** — how to *use* a deployed relay from a fresh session.
6. **[docs/deploy.md](docs/deploy.md)** — how to deploy/operate a new appliance.

## Using a deployed relay (the common case)
The relay is **off by default**. To use it from a fresh Claude session:
1. Bring it up with `~/.config/mage-hands/relay.sh up` (scoped passwordless sudo on the NAS;
   starting it is approval-gated). See getting-started.md.
2. Start a **new** Claude Code session — remote MCP servers load at session start, so its
   tools appear as `mcp__<name>__*` (e.g. `mcp__kappa__system_info`).
3. Prefer Tier-A inspection tools (they auto-run). Mutation (`restart_*`), raw exec (`run`),
   and starting the relay require an **approval prompt** — that's intentional. For `run()`,
   **always dry-run first** (call without `exec_token`), show the user the intended command,
   then execute by replaying the token.

If the server shows disconnected in `/mcp`, the relay is probably down — that's expected when
idle; bring it back up.

## Working conventions
- **Secrets never get committed.** `.env`, `*.token`, and `logs/` are gitignored. The bearer
  token lives only in the appliance `.env` (chmod 600) and the Mac token file
  (`~/.config/nas-relay/<name>.token`); it is also stored literally in `~/.claude.json` by
  `claude mcp add` — keep that out of backups.
- **The security model is isolation + auth + ephemerality + audit, not sandboxing.** A running
  relay is root on its host by design. Don't add capabilities that assume containment.
- **Put cross-cutting logic in `common/`, not in an appliance.** Auth, audit, the `run()` gate,
  and the read policy are inherited; an appliance should only add a Runner + tools.
- **The relay binds loopback only**; `tailscale serve` is the only ingress. Never bind a
  routable interface, never use `tailscale funnel` (that's public).
- **Deploy privileged stacks via SSH `docker compose`** — Synology's Container Manager GUI
  can't set `privileged`.

## Environment
Authored on macOS (Apple Silicon, 64 GB), `uv`-first Python tooling. The relay image is
`python:3.12-slim` + `util-linux` (for `nsenter`) + `fastmcp`. Targets are x86 Synology
(DSM 7.2+) for now.
