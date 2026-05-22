# Agent Guide

Context for AI agents (Claude Code, Copilot, etc.) working on this project.

## What This Project Does

Runs small **MCP relays** on home-lab appliances so Claude (on a Mac) can administer them
remotely. The model never runs on the appliance — only a thin relay that executes structured
tool calls locally and returns JSON. A relay is ephemeral (off by default) and, while up, has
root on its host; the safety properties are network isolation, a bearer token, ephemerality,
and a forensic audit log.

## Key Files to Read First

1. **`common/mage_hands_core/server.py`** — `build_server()` wires a FastMCP server with token
   auth, the audit middleware, and a lifespan flush. Start here to see how a relay is assembled.
2. **`common/mage_hands_core/exec.py`** — the `Runner` strategies (`ShellRunner`,
   `NsenterRunner`) and `register_run_tool()`, the gated Tier-C `run()` (dry-run → one-time
   `exec_token` replay + catastrophic-pattern `DEFAULT_DENY`).
3. **`common/mage_hands_core/policy.py`** — `PathPolicy` + `register_read_file()`: the allow/deny
   read policy and the join-then-resolve traversal guard.
4. **`common/mage_hands_core/audit.py`** — forensic JSONL logging, the Tailscale-identity
   allowlist enforcement, and the atomic `last_activity` ping the idle watchdog reads.
5. **`synology-hands/server.py`** — a complete appliance: imports the core, registers Synology
   Tier-A/B tools, and wires `read_file` + `run()` to an `NsenterRunner`.
6. **`ARCHITECTURE.md`** — request lifecycle, security model, tool tiers, audit schema, env config.

## Common Tasks

### Adding a tool to an appliance
Register a function on the `mcp` returned by `build_server()`:
```python
@mcp.tool()                                   # Tier A (read-only)
def smart_attributes(device: str) -> dict:
    """Full SMART attributes for a disk."""
    return host.run(["smartctl", "-A", device])

@mcp.tool(annotations={"destructiveHint": True})   # Tier B (mutation)
def stop_container(name: str) -> dict:
    return host.run(["docker", "stop", name])
```
Keep raw arbitrary execution in the single gated `run()` — don't add ad-hoc shell-exec tools.

### Adding a new appliance (e.g. router-hands)
The core carries all security logic; an appliance supplies a **Runner** + **tools**:
```python
cfg  = Config.from_env()
mcp  = build_server("router-hands", INSTRUCTIONS, cfg)
host = ShellRunner()      # or NsenterRunner, or a new SSHRunner added to common/exec.py
# @mcp.tool() ... router tools ...
register_read_file(mcp, PathPolicy(allow=[...], deny=[...]), fs_reader("/host"))
register_run_tool(mcp, host)
run_server(mcp, cfg)
```
If the relay reaches the target over SSH instead of running on it, add an `SSHRunner` to
`common/exec.py` (implements `Runner.run`). For non-filesystem reads, `register_read_file`
already accepts an arbitrary `reader` callable. See `router-hands/README.md`.

### Tuning the catastrophic-command denylist
`DEFAULT_DENY` in `common/exec.py` is a regex backstop (it is *not* a complete safety
guarantee). It blocks whole-pool/root wipes — including trailing-slash and glob forms
(`rm -rf /`, `/*`, `/volume1`, `/volume1/`, `/volume1/*`), `mkfs`, `dd of=/dev/*`, destructive
`mdadm`, recursive chmod/chown on `/`, partition tools, and `synostorage --delete`. Targeted
deletes *under* a volume (e.g. `/volume1/docker/app/cache`) are intentionally allowed. Pass
`deny_patterns=` to `register_run_tool()` to override.

### Tuning the read policy
Set `READ_ALLOW` / `READ_DENY` in the appliance `server.py`. The deny list should cover secret
paths (`/etc/shadow`, ssh/gnupg keys, Tailscale state, docker secrets). `read_file` is the most
likely accidental exfiltration vector — keep deny tight and allow narrow.

### Deploying / operating
See **[docs/deploy.md](docs/deploy.md)**. Day-to-day, start/stop from the Mac with
`~/.config/mage-hands/relay.sh up|down` (uses the NAS's scoped passwordless sudo); the idle
watchdog auto-stops it.

### Granting scoped passwordless start/stop
`scripts/install-sudo.sh` (run as root on the appliance) installs root-owned copies of the
lifecycle scripts to `/usr/local/sbin/mage-hands-relay-{up,down}` and a `/etc/sudoers.d/mage-hands`
NOPASSWD rule for **only those two paths**. The copies must live somewhere the relay user can't
edit *and* can't directory-swap — `/usr/local/sbin` works because `/usr/local` is root-owned.
Re-run after editing the lifecycle scripts. Everything else stays password-gated by design.

### Setting the Claude Code approval model
In `~/.claude/settings.json`, `permissions.allow` lists the read-only relay tools (auto-run) and
`permissions.ask` lists `restart_container` / `restart_service` / `run` plus the
`relay.sh` helper (approval each call). Adjust the lists to change what prompts.

### Rotating the token
Generate a new token (`openssl rand -hex 32`), update the appliance `.env` and recreate the
container, update the Mac token file, and re-run `claude mcp add` (or move to a `headersHelper`
script). Use a **separate token per appliance**.

## Deployed Appliances

| Name | Host | Hardware / OS | MCP URL | Notes |
|------|------|---------------|---------|-------|
| `kappa` (synology-hands) | `kappa.local` | Synology 718+ (apollolake), DSM 7.2.1 x86_64 | `https://kappa.<tailnet>.ts.net/mcp` | admin user `magehands`; deploy dir `/volume1/docker/mage-hands`; token at `~/.config/nas-relay/kappa.token`; `ALLOWED_USERS` = your Tailscale login; scoped passwordless sudo installed; Mac start/stop via `~/.config/mage-hands/relay.sh` + approval rules in `~/.claude/settings.json` |

## Important Patterns

- **The relay binds `127.0.0.1:8787` only.** `tailscale serve` (HTTPS :443, tailnet-private)
  is the sole ingress. This is also why the injected `Tailscale-User-*` identity headers are
  trustworthy — Serve strips spoofed inbound copies and the backend isn't reachable directly.
- **Host execution is via `nsenter -t 1`** (requires `privileged` + `pid: host`). It uses the
  *host's* binaries, so the container only ships `nsenter` + Python. Container env vars do not
  leak into host execution.
- **`run()` is a two-call state machine.** First call (no `exec_token`) returns a preview + a
  one-time token bound to the exact command (5-min TTL). Second call replays the token to
  execute. A changed command or expired token is refused.
- **Audit-first.** Every tool call logs a JSON line (timestamp, correlation id, node, caller
  identity, tool, args, status, ms) and updates `last_activity`. Logs dir is `chmod 700` root.
- **Ephemerality is a control, not a convenience.** `restart: "no"` + the idle watchdog keep
  the root-capable surface from lingering.
- **Two extra gates wrap the relay:** scoped passwordless sudo on the NAS (lifecycle scripts
  only — destructive sudo still needs the password) and Mac-side approval prompts for mutation /
  raw exec / relay start. Read-only inspection stays frictionless.

## Things to Watch Out For

- **Synology home-dir perms.** Key auth fails unless `~`, `~/.ssh` are `700` and
  `authorized_keys` is `600`. `ssh-copy-id` doesn't fix the home dir itself.
- **Synology sudo `secure_path`** excludes `/usr/local/bin` and the Tailscale package dir, so
  `docker`/`tailscale` aren't found via `sudo`. Scripts resolve full binary paths.
- **Bind-mount sources must pre-exist.** Synology's daemon won't auto-create `./logs`; create it
  before `compose up`.
- **`/etc/crontab` is DSM-managed** (regenerated from Task Scheduler). Don't hand-edit — add the
  idle watchdog via the Task Scheduler GUI.
- **Caller identity ≠ git email.** `ALLOWED_USERS` is the Tailscale login (`tailscale status`).
- **Deploy empty, then tighten.** Bring up with `ALLOWED_USERS` empty, confirm the real identity
  in the audit log, then set it — otherwise a wrong guess locks you out.

## Dependencies

Relay image: `python:3.12-slim` + `util-linux` (nsenter) + `fastmcp` (>=3,<4, pulls `mcp`,
`pydantic`, `uvicorn`, `starlette`, `authlib`). Appliance host needs Docker/Container Manager
and Tailscale. The Mac needs Claude Code; the smoke test needs `fastmcp` (run via `uv`).
