# mage-hands

Ephemeral, privileged **MCP relays** that let Claude Code administer home-lab appliances it
can't (or shouldn't) run an agent on directly. The model runs on your Mac; each appliance runs
only a tiny relay that executes structured requests locally and returns results.

The project is two layers:

| Layer | What it is | Lives in |
|-------|------------|----------|
| **Core** (`mage_hands_core`) | Reusable, pip-installable relay framework: token auth, forensic audit, the gated `run()` tool, and the read path policy. The security machinery lives here once, so every appliance inherits it. | `common/` |
| **Appliances** | Thin servers that pick an executor (how to run on the target) and register target-specific tools. `synology-hands` administers a Synology NAS (privileged container + `nsenter`); `router-hands` administers an ASUS Asuswrt-Merlin router over SSH (`SSHRunner` + a Tailscale sidecar). | `synology-hands/`, `router-hands/` |

> **Heads-up:** the relay is **OFF by default**. You bring it up for a session and it
> auto-stops when idle. While up it is effectively root on the target — safety comes from the
> *relay's* network isolation (loopback bind + tailnet-only `tailscale serve`, never WAN/funnel),
> a bearer token, ephemerality, and a forensic audit log, plus dry-run/replay gating on raw
> execution. It is **not** sandboxed; that's deliberate. (The *host's* own WAN exposure —
> QuickConnect, DDNS, port-forwarding — is a separate concern the relay doesn't control; the
> `internet_exposure` tool surfaces it.)

## How it works

```
┌──────────────┐  https://<nas>.<tailnet>.ts.net/mcp   ┌─────────────────────────────┐
│ Claude (Mac) │ ─── Tailscale Serve (TLS, ACL'd) ───► │ appliance (e.g. Synology)   │
│  MCP client  │ ◄── structured JSON ─────────────────│  Tailscale + Serve :443     │
└──────────────┘  Authorization: Bearer <token>        │   ↓ proxy 127.0.0.1:8787    │
                  (Serve injects Tailscale-User-*)      │  relay container (ephemeral)│
                                                        │   privileged, pid:host, /:/  │
                                                        │   FastMCP /mcp → nsenter →  │
                                                        │   host (docker, smartctl…)  │
                                                        └─────────────────────────────┘
```

1. The relay runs in a privileged container and drives the host via `nsenter -t 1` — so it
   uses the host's own toolchain (docker, smartctl, syno*), no docker-socket mount.
2. `tailscale serve` terminates HTTPS on the tailnet (never the public internet) and proxies
   to the relay bound on **loopback only**.
3. Your Mac connects to it as a normal remote MCP server; tools appear as `mcp__<name>__*`.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the request lifecycle, security model, tool
tiers, and audit schema; **[AGENTS.md](AGENTS.md)** for a contributor/agent guide;
**[lessons.md](lessons.md)** for deployment lessons and Synology gotchas.

## Quick start

**Already deployed and just want to use it?** → **[docs/getting-started.md](docs/getting-started.md)**
(bring the relay up, then the tools auto-load in a fresh Claude session).

**Deploying to a new appliance?** → **[docs/deploy.md](docs/deploy.md)** (end-to-end runbook).

### Prerequisites
- An appliance that runs Docker/Container Manager (x86 Synology DSM 7.2+ for `synology-hands`).
- [Tailscale](https://tailscale.com/) on both the appliance and your Mac, with MagicDNS +
  HTTPS enabled on the tailnet.
- [Claude Code](https://claude.com/claude-code) on your Mac.

### Tool tiers
The relay exposes capabilities in three tiers (see ARCHITECTURE.md):

| Tier | Nature | Examples | Gating |
|------|--------|----------|--------|
| **A** | inspection (read-only) | `system_info`, `disk_usage`, `storage_health`, `list_containers`, `internet_exposure`, `performance`, `pending_updates`, `firewall_status`, `firewall_rules`, `firewall_diagnose`, `read_file` | none; `read_file` is allow/deny policied |
| **B** | controlled mutation | `restart_container`, `restart_service`, `firewall_enable`, `firewall_disable`, `firewall_reload`, `firewall_set_rules` | typed args, audited; `firewall_set_rules` is lock-out-guarded |
| **C** | raw root exec | `run(command, exec_token)` | dry-run → one-time replay token + catastrophic-pattern denylist |

## Repository layout

```
mage-hands/
├── common/                       # mage_hands_core — the reusable relay framework
│   ├── pyproject.toml
│   └── mage_hands_core/
│       ├── server.py             # build_server(): FastMCP + auth + audit + lifespan
│       ├── auth.py               # StaticTokenVerifier (real 401 at the transport layer)
│       ├── audit.py              # forensic JSONL log + identity allowlist + activity ping
│       ├── exec.py               # Runners (Shell/Nsenter) + gated run() (denylist + token)
│       ├── policy.py             # PathPolicy + policied read_file
│       └── config.py             # env-driven Config
├── synology-hands/               # appliance #1: Synology NAS
│   ├── server.py                 # Tier A/B tools; registers read_file + run()
│   ├── Dockerfile  compose.yaml  .env.example
│   ├── scripts/                  # relay-up/down · idle-watchdog · tailscale-update · install-sudo · smoke-test.py
│   └── README.md
├── router-hands/                 # appliance #2: ASUS Merlin router (SSHRunner + Tailscale sidecar)
│   ├── server.py                 # Tier A/B router tools; SSHRunner; read_file + run() (on by default)
│   ├── Dockerfile  compose.yaml  serve.json  .env.example
│   ├── scripts/                  # relay-up/down · idle-watchdog · install-sudo · smoke-test.py
│   └── README.md
├── net-monitor/                  # standalone (NOT an MCP relay): always-on internet connectivity logger on kappa
│   ├── compose.yaml  monitor.sh  summary.sh
│   └── README.md
├── docs/
│   ├── getting-started.md        # use a deployed relay from a fresh Claude session
│   ├── deploy.md                 # deploy/operate a new appliance
│   └── maintenance.md            # update Tailscale, scheduled tasks, troubleshooting
├── ARCHITECTURE.md  AGENTS.md  CLAUDE.md  lessons.md
├── CONTRIBUTING.md               # how to contribute + security-first ground rules
├── LICENSE                       # MIT
└── README.md
```

## License

[MIT](LICENSE) © Michael Timpe.
