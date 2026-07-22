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
def smart_attributes(
    device: Annotated[str, Field(
        description="Block device path from storage_health's scan, e.g. '/dev/sata1'."
    )],
) -> dict:
    """Full SMART attributes for one disk (smartctl -A). Use storage_health first to list
    devices. Returns {rc, stdout, stderr}."""
    return host.run(["smartctl", "-A", device])

@mcp.tool(annotations={"destructiveHint": True})   # Tier B (mutation)
def stop_container(
    name: Annotated[str, Field(
        description="Exact container name as shown by list_containers, e.g. 'calibre-web'."
    )],
) -> dict:
    """Tier B — stop a container (docker stop). Returns {rc, stdout, stderr}."""
    return host.run(["docker", "stop", name])
```
Keep raw arbitrary execution in the single gated `run()` — don't add ad-hoc shell-exec tools.

### Tool design checklist (MCP hygiene — apply to EVERY new tool)
Distilled from live agent-vs-server testing (https://tengli.dev/posts/mcp-servers-failing-agents.html):
the dominant real-world failure is undocumented parameters, and fuzzy catalogs measurably degrade
tool selection *and* the model's ability to refuse out-of-scope work.

1. **Every parameter gets a `Field(description=...)`** stating meaning, format, and one concrete
   example value (`"e.g. 'calibre-web'"`). Bare `name: str` params are the #1 failure mode in
   production MCP servers — schemas look fine, models still guess.
2. **Docstring = what it does + when to use it + what it returns.** Include cross-tool routing
   where relevant ("use list_containers first to find the name").
3. **Fixed value sets are `Literal[...]`/enum in the schema**, not prose. Derive the runtime
   allowlist from the Literal (see `ServiceName` in router-hands) so schema and check can't drift;
   keep the runtime check as a backstop.
4. **Errors must name the offending parameter** and how to fix it, so the model self-corrects in
   one turn: refusal dicts carry `"parameter": "<name>"` + a reason quoting the bad value;
   policy exceptions name `'path'`, the value, and the allowed roots.
5. **Naming: one convention, no near-synonyms.** Match the existing catalog (`<object>_<facet>`
   for inspection: `firewall_status`, `wan_status`; `verb_object` for mutation: `restart_service`,
   `reboot_router`). Never add a tool whose name could be confused with an existing one
   (`extract` vs `scrape` is the canonical collision) — extend the existing tool instead.
6. **Small catalog, sharp scope.** Every tool's schema is serialized into every request; prefer
   one well-documented tool with a parameter over three overlapping ones. If a tool is
   out-of-scope for a task, its description should make that obvious enough that the model
   declines rather than force-fits.
7. **Declare `required` honestly** — FastMCP derives it from defaults, so don't give a parameter
   a default it shouldn't have (e.g. `confirm: bool = False` is right; a defaulted target name
   is not).

### Adding a new appliance
The core carries all security logic; an appliance supplies a **Runner** + **tools**:
```python
cfg  = Config.from_env()
mcp  = build_server("router-hands", INSTRUCTIONS, cfg)
host = NsenterRunner(cap=cfg.output_cap)         # drive its OWN host (NAS), OR:
# host = SSHRunner.from_env(cap=cfg.output_cap)  # drive a REMOTE target over SSH (router)
# @mcp.tool() ... target tools ...
register_read_file(mcp, PathPolicy(allow=[...], deny=[...]),
                   fs_reader("/host"))           # OR runner_reader(host) for SSH/non-mounted targets
register_run_tool(mcp, host, deny_patterns=DEFAULT_DENY + cfg.run_deny_extra)
run_server(mcp, cfg)
```
Two transports exist (both implement `Runner.run`): `NsenterRunner` (a privileged container drives
its own host) and `SSHRunner` (the relay runs elsewhere and reaches the target over SSH, fronted by
a Tailscale sidecar so it gets its own MagicDNS node). `register_read_file` takes any `reader`
callable — use `runner_reader(host)` when there's no mounted filesystem. `router-hands` is the
worked SSH example; see `router-hands/README.md`.

### Tuning the catastrophic-command denylist
`DEFAULT_DENY` in `common/exec.py` is a regex backstop (it is *not* a complete safety
guarantee). It blocks whole-pool/root wipes — including trailing-slash and glob forms
(`rm -rf /`, `/*`, `/volume1`, `/volume1/`, `/volume1/*`), `mkfs`, `dd of=/dev/*`, destructive
`mdadm`, recursive chmod/chown on `/`, partition tools, and `synostorage --delete`. Targeted
deletes *under* a volume (e.g. `/volume1/docker/app/cache`) are intentionally allowed. It also
refuses availability commands at command position (`reboot`, `shutdown`, `poweroff`, `halt`,
`init 0`, `kill -1`). Operators **append** patterns via `RUN_DENY_EXTRA` (never replace); an
appliance composes its own list via `deny_patterns=` (router-hands passes
`DEFAULT_DENY + ROUTER_DENY_EXTRA + cfg.run_deny_extra`, adding firmware/nvram-erase/mtd cases plus
the indirect Merlin reboot paths the command-position anchor misses — `service reboot`, `init 6`,
`busybox reboot`, `rc reboot`, `killall rc` — so the gated `reboot_router` stays the only intended
reboot route now that router `run()` is on by default).

### Tuning the read policy
Set `READ_ALLOW` / `READ_DENY` in the appliance `server.py` (the baseline). Per-box, **append**
roots at runtime via `READ_ALLOW_EXTRA` / `READ_DENY_EXTRA` (set-but-empty is a no-op, so a copied
compose stack can't silently drop a deny path); `READ_POLICY_OVERRIDE=1` makes the `*_EXTRA` lists
fully replace the defaults (logged at startup). The deny list should cover secret paths
(`/etc/shadow`, ssh/gnupg keys, Tailscale state, docker secrets). `read_file` is the most likely
accidental exfiltration vector — keep deny tight and allow narrow.

### Deploying / operating
See **[docs/deploy.md](docs/deploy.md)**. Day-to-day, start/stop from the Mac with
`~/.config/mage-hands/relay.sh <appliance> up|down` (uses the NAS's scoped passwordless sudo);
the idle watchdog auto-stops it. Shell shortcuts in `~/.config/mage-hands/relay-aliases.sh`
(sourced by `~/.zshrc`) wrap these — `start-kappa-relay`/`start-alpha-relay`/`start-router-relay`
(+ `stop-*`), and `start-all-relays`/`stop-all-relays` bring **all three** relays (kappa + alpha +
router1) up/down at once.

### Granting scoped passwordless start/stop
`scripts/install-sudo.sh` (run as root on the appliance) installs root-owned copies of the
lifecycle scripts to `/usr/local/sbin/mage-hands-relay-{up,down}` and a `/etc/sudoers.d/mage-hands`
NOPASSWD rule for **only those two paths**. The copies must live somewhere the relay user can't
edit *and* can't directory-swap — `/usr/local/sbin` works because `/usr/local` is root-owned.
Re-run after editing the lifecycle scripts. Everything else stays password-gated by design.

### Setting the Claude Code approval model
`relay.sh <appliance> up` is a **single command that enables full tool functionality**: it appends
`mcp__<appliance>` to `permissions.allow` in `~/.claude/settings.json` (that whole-server rule
matches every tool from the appliance — read-only audit/diagnose *and* mutation `restart_*` /
`firewall_*` / gated `run`, including tools added later) and then starts the relay. The only rule
you add by hand is `Bash(.../relay.sh:*)` so the helper itself runs unprompted. Server-side controls
(catastrophic denylist, two-call `exec_token` gate, identity allowlist, audit log,
`firewall_set_rules`' lock-out guard) are the real safety, not per-call prompts. To gate mutation
with prompts instead, drop the `enable_all_tools` call from `relay.sh` and enumerate only read-only
tools in `allow`.

### Rotating the token
Generate a new token (`openssl rand -hex 32`), update the appliance `.env` and recreate the
container, update the Mac token file, and re-run `claude mcp add` (or move to a `headersHelper`
script). Use a **separate token per appliance**.

## Deployed Appliances

| Name | Host | Hardware / OS | MCP URL | Notes |
|------|------|---------------|---------|-------|
| `kappa` (synology-hands) | `kappa.local` | Synology 718+ (apollolake), DSM 7.2.1 x86_64 | `https://kappa.<tailnet>.ts.net/mcp` | admin user `magehands`; deploy dir `/volume1/docker/mage-hands`; token at `~/.config/nas-relay/kappa.token`; `ALLOWED_USERS` = your Tailscale login; scoped passwordless sudo installed; Mac start/stop via `~/.config/mage-hands/relay.sh kappa up\|down` + approval rules in `~/.claude/settings.json` |
| `alpha` (synology-hands) | `alpha.local` | Synology 1517+ (avoton), DSM 7.3.1 x86_64; 5× 10TB → 2× RAID5 → LVM `volume_1` ~37 TiB; **SSD cache** 2× Intel D3-S4510 240GB M.2 SATA (M2D17) in RAID1 read-write/writeback (DSM `nvc1`/`nvc2`) | `https://alpha.<tailnet>.ts.net/mcp` | same setup mirrored from kappa; token at `~/.config/nas-relay/alpha.token`; `mcp__alpha__*` permission rules added; start/stop `~/.config/mage-hands/relay.sh alpha up\|down` |
| `router1` (router-hands) | runs on `kappa.local` | ASUS Asuswrt-Merlin router, reached over SSH | `https://router1.<tailnet>.ts.net/mcp` | **deployed & operational (2026-06-16); provisioned per [router-hands/README.md](router-hands/README.md)**. SSHRunner relay container on kappa + Tailscale **sidecar** node; unprivileged; SSH key in `router-hands/secrets/`; `BIND_HOST=127.0.0.1`/`PORT=8788`; synology-parity Tier-A tools (`disk_usage`/`performance`/`pending_updates`/`internet_exposure`) + gated `reboot_router`; `run()` **on by default** (`ROUTER_ENABLE_RUN=false` to disable; router denylist also closes indirect reboot paths so `reboot_router` is the only intended one); lifecycle `mage-hands-router-relay-{up,down}`; `relay.sh router1 up\|down` (SSHes to kappa, not the router) |

**Status (2026-05-22):** both NAS boxes on Tailscale **1.98.2**; per-box DSM Task Scheduler jobs
active — idle-watchdog (every 5 min) and tailscale-update (weekly); relays **off by default**.
Three read-only Tier-A tools added — `internet_exposure`, `performance`, `pending_updates` (and the
`run()`/Tier-A output cap is now env-tunable via `OUTPUT_CAP`). **DSM firewall tools added**
(`synology-hands/firewall.py`): Tier-A `firewall_status` / `firewall_rules` / `firewall_diagnose`
and Tier-B `firewall_enable` / `firewall_disable` / `firewall_reload` / `firewall_set_rules`. Reads
and writes go through DSM's own oracles (`synofirewall --info`, the `SYNO.Core.Security.Firewall*`
webapi) — never hand-encoded integer rule codes — and `firewall_set_rules` carries a **lock-out
guard** (simulates first-match rule evaluation and refuses any change that would deny SSH/DSM admin
from your LAN). Verified empirically on kappa (firewall is currently **off** on both boxes — see the
audit's standing P1-3 recommendation to enable it with an allow-list). **Security remediation:**
QuickConnect was found **enabled on both boxes** (relaying DSM/SSH; the 2026-05 audit missed it —
see [docs/audit-2026-05.md](docs/audit-2026-05.md)). **Both boxes remediated 2026-05-22:**
QuickConnect **disabled** and SSH **password auth turned off** (key-only; verified) on kappa **and**
alpha; auto-block confirmed on. On **alpha** additionally: app containers (jackett/radarr/sabnzbd-1/
sonarr-1/transmission) set to `restart=unless-stopped`, the `watchtower` container (docker.sock:rw +
net=host) **removed**, and Transmission RPC **whitelisted** to localhost+LAN. (Reminder: DSM 7 uses
`synosystemctl`, not `synoservicectl`; QuickConnect lives in `/usr/syno/etc/synorelayd/`.)

**New scripts + schedules (alpha):** `plex-update.sh` (Plex bumped 1.43.1→1.43.2; weekly Task
Scheduler **id 18**, Sun 04:00) and `ups-healthcheck.sh` (daily **id 19**, 09:00, **emails on a DOWN
result**) — both created via the `synowebapi SYNO.Core.TaskScheduler` recipe in
[docs/maintenance.md](docs/maintenance.md). The alpha **UPS** (CyberPower LE1000DG) read as
dead because its USB interface wasn't enumerating; after a **physical port move** it's **online** and
DSM auto-loaded `usbhid-ups`. **Approval model:** `relay.sh <appliance> up` is now a
**single command that enables full tool functionality** — it appends the whole-server rule
`mcp__<appliance>` to `permissions.allow` in `~/.claude/settings.json` (covering every tool from that
box, including the new `firewall_*` ones) and then starts the relay; only `Bash(.../relay.sh:*)` is
added by hand. So all tools auto-run with no per-call prompts — the relay's own catastrophic-pattern
denylist, two-call `exec_token` gate, identity allowlist, audit log, and `firewall_set_rules`'
lock-out guard are the server-side safety. **SSD cache reviewed 2026-05-22** (alpha): both Intel S4510
members healthy at ~99–100% remaining life, all error counters 0, PLP self-test passing; it's a
read-write/writeback RAID1 cache fronting `volume_1` — write-hit ~64% (useful), read-hit ~2%
(sequential media I/O bypasses by design). How to inspect it: [docs/maintenance.md](docs/maintenance.md)
*"Checking SSD cache health, wear & effectiveness"* (the wear data is in `/run/synostorage/disks/`,
**not** `smartctl -d nvme`, which the M.2-SATA cache devices reject).

**Auto-updates reviewed + telemetry added (2026-06-16):** the alpha DSM Task Scheduler update
scripts are **verified working** — `tailscale-update.sh` (**id 15**, weekly Tue 00:00 → 1.98.4) and
`plex-update.sh` (**id 18**, weekly Wed 12:00 → 1.43.2.10687), both bypassing Package Center.
Container auto-updates were re-architected: the disabled persistent `watchtower-compose` Container
Manager project was **removed**, replaced by `synology-hands/scripts/watchtower-update.sh` — a
one-shot `docker run --rm … watchtower --run-once` registered as **id 20** (weekly Tue 12:00; the
script gates to the **first Tuesday** of the month). `WATCHTOWER_LABEL_ENABLE=false`, so one pass
updates every registry-image container (shelfmark, calibre-web-automated, the *arr stack), then
exits and self-removes. On **kappa**, **`router-monitor`** was deployed — an always-on logger that
SSHes to `router1` (reusing the router-hands key, read-only) writing per-day health JSONL +
edge-events **and mirroring the router's `/jffs/syslog.log` off-box before its daily rotation
discards it** (the spring firmware episode left no trail); it complements `net-monitor`
(internet-path quality). See [router-monitor/README.md](router-monitor/README.md).
`router-hands` (`router1`) is **deployed & operational (2026-06-16)** — its relay runs on
`kappa` and reaches the ASUS Merlin router over SSH; deploy per
[router-hands/README.md](router-hands/README.md) (provision the SSH key + `TS_AUTHKEY` on kappa,
then confirm the bring-up's `SSH egress: PASS`). It now has synology-parity Tier-A tools
(`disk_usage`/`performance`/`pending_updates`/`internet_exposure`), a gated `reboot_router`, and
`run()` **on by default** (`ROUTER_ENABLE_RUN=false` to disable). On first deploy, verify the
Merlin-specific assumptions in [router-hands tests + the plan's live checklist] (esp. `sshd_enable`→
WAN mapping, `vts_rulelist` field order, CPU-temp source) before trusting `internet_exposure` output.
**Security hardening pass (2026-06-10):** a code review (the project was flagged for cybersecurity
safety) drove a round of verified bugfixes + hardening across `common/` and both appliances. The
headline fix closed a **read-policy symlink bypass** — `fs_reader` resolved symlinks but only
re-checked `/host` containment, so a relative symlink under an allowed root
(`/volume1/link -> ../../etc/shadow`) could read outside the allow/deny lists; it now re-runs
`PathPolicy.check()` on the resolved path (see lessons.md). Also: byte-accurate output truncation,
bounded audit `args`, `last_activity` touched at call **start** too (idle watchdog can't kill a
long `run()` mid-call), expired `exec_token` GC, an `sh -c '<reboot>'` denylist pattern, a 16-char
`RELAY_TOKEN` floor, `SSHRunner` fail-fast on missing/empty `known_hosts` when strict, `firewall_set_rules`
`default_policy` validation, `_iowait_delta` clamped to [0,100], `shlex.quote` on `_nvram_many` keys,
and `${PORT}`-parameterized compose healthchecks. **kappa + alpha redeployed and live-verified**
(the symlink now returns "denied by read policy"); router-hands got the fixes in-repo but is still
undeployed. 129 unit tests green.

**To resume in a fresh session:** start a relay with `~/.config/mage-hands/relay.sh <kappa|alpha> up`,
open a new Claude session (tools auto-load as `mcp__<name>__*`; read-only auto-runs, mutation/exec
prompt), do the work, then `~/.config/mage-hands/relay.sh <name> down`. See
[docs/getting-started.md](docs/getting-started.md) and [docs/maintenance.md](docs/maintenance.md).

**Status (2026-06-16):** `router1` (router-hands) is **deployed & operational** — used it this
session to add a Merlin **dnsmasq local-DNS record** (`cwa.klab-alpha.direct.quickconnect.to` →
alpha `.247`) so the LAN resolves CWA's Kobo-sync host. Via **alpha** (synology-hands) configured
**Kobo sync** for the book stack: alpha runs **Calibre-Web-Automated** (`:8083`) + **shelfmark**
(`:8084`) + Audiobookshelf (`:13378`) — the **home-infra runbook** is the service source-of-truth.
CWA is fronted by a **manual nginx SNI vhost** (`/usr/local/etc/nginx/sites-enabled/cwa-kobo.conf`,
wildcard QuickConnect cert + raised `proxy_buffer_size`) because the DSM
`SYNO.Core.AppPortal.ReverseProxy` `create` API rejected the entry (err **4151**) — note a DSM OS
update can regenerate nginx and wipe that vhost. Also disabled CWA's per-user **auto-send** (it had
been pushing every ingested book to a Kindle). The four Mac shortcuts (`start-kappa-relay`,
`start-alpha-relay`, `start-router-relay`, `start-all-relays`) verified working.

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
**router-hands** swaps `util-linux` for `openssh-client` (it SSHes out, doesn't nsenter) and adds
a `tailscale/tailscale` sidecar container so it gets its own tailnet node.
