# Architecture

## System Overview

A `mage-hands` deployment has three moving parts: the **Claude client** (your Mac), a
**Tailscale Serve** front door, and a **relay container** on the appliance. The model stays on
the Mac; the relay only routes structured tool calls to the host and returns JSON.

```
┌──────────────┐                                   ┌────────────────────────────────────┐
│ Claude (Mac) │                                   │            APPLIANCE (host)          │
│  MCP client  │                                   │                                      │
│              │   https://<nas>.<tailnet>.ts.net  │  ┌────────────────────────────────┐  │
│  reads token │ ───── Tailscale (WireGuard, ─────►│  │ tailscale serve :443 (TLS term)│  │
│  from        │        ACL-restricted) ◄──────────│  │   strips spoofed identity hdrs │  │
│  ~/.config/  │                                   │  │   injects Tailscale-User-*     │  │
│  nas-relay/  │   Authorization: Bearer <token>   │  └───────────────┬────────────────┘  │
└──────────────┘                                   │        proxy → 127.0.0.1:8787        │
                                                   │  ┌───────────────▼────────────────┐  │
                                                   │  │ relay container (ephemeral)     │  │
                                                   │  │  FastMCP /mcp                   │  │
                                                   │  │  ① StaticTokenVerifier → 401    │  │
                                                   │  │  ② AuditMiddleware (identity,   │  │
                                                   │  │     allowlist, JSONL, activity) │  │
                                                   │  │  ③ tool dispatch                │  │
                                                   │  └───────────────┬────────────────┘  │
                                                   │      nsenter -t 1 (privileged,       │
                                                   │      pid:host) → host namespaces     │
                                                   │  ┌───────────────▼────────────────┐  │
                                                   │  │ host toolchain: docker, smartctl,│ │
                                                   │  │ synoservicectl, sh, /:/host fs   │ │
                                                   │  └─────────────────────────────────┘ │
                                                   └──────────────────────────────────────┘
```

## Request lifecycle

1. **Mac → Serve.** Claude sends an MCP-over-HTTP request to `https://<nas>.<tailnet>.ts.net/mcp`
   with `Authorization: Bearer <token>`. Traffic is WireGuard-encrypted inside the tailnet and
   gated by the tailnet ACL.
2. **Serve → relay.** `tailscale serve` terminates TLS, **strips any inbound `Tailscale-User-*`
   headers and injects the verified caller identity**, then proxies to the relay on loopback.
3. **Auth (①).** FastMCP's `StaticTokenVerifier` checks the bearer token and returns an HTTP 401
   before any tool runs.
4. **Audit + identity (②).** `AuditMiddleware.on_call_tool` reads `Tailscale-User-Login`,
   optionally enforces the `ALLOWED_USERS` allowlist, assigns a correlation id, runs the tool,
   then writes one JSON audit line and updates `last_activity`.
5. **Execution (③).** The tool runs. Inspection/mutation tools and `run()` shell out through a
   **Runner**; on the NAS that's `NsenterRunner`, which prefixes `nsenter -t 1 -m -u -i -n -p --`
   to enter the host namespaces and use the host's own binaries.

## Core / appliance split

`common/mage_hands_core` is an installable package; appliances depend on it.

| Module | Responsibility |
|--------|----------------|
| `config.py` | `Config.from_env()` — `RELAY_TOKEN`, `NODE_ID`, `ALLOWED_USERS`, `AUDIT_DIR`, bind host/port/path, graceful timeout. |
| `auth.py` | `build_token_verifier()` — probes fastmcp for `StaticTokenVerifier` (import path varies by build) and returns a single-token verifier. |
| `audit.py` | `setup_audit()` (rotating JSONL), `touch_activity()` (atomic), `AuditMiddleware` (identity allowlist + forensic log), `truncate()`. |
| `exec.py` | `Runner` protocol, `ShellRunner` / `NsenterRunner` / `SSHRunner`, `DEFAULT_DENY`, `register_run_tool()` (the gated Tier-C `run()`). |
| `policy.py` | `PathPolicy` (allow/deny + lexical normalize), `fs_reader()` (join-then-resolve traversal guard), `runner_reader()` (read via a Runner — for SSH/non-mounted targets), `register_read_file()`. |
| `server.py` | `build_server()` (FastMCP + auth + lifespan flush + audit middleware), `run_server()`. |

An appliance (`synology-hands/server.py`) is then just: build the server, choose a Runner,
register tools, register `read_file` + `run()`, and `run_server()`. The Runner is the transport
seam: `NsenterRunner` (drive the host from a privileged container) and `SSHRunner` (drive a remote
target over SSH — the router pattern) are interchangeable to the gating/tool code above them.

## Security model

The relay is intentionally all-powerful: `privileged` + `pid: host` + `/:/host`. **Once up it
is effectively root on the host.** Security is therefore *not* capability sandboxing — it is
four layers plus execution friction:

1. **Isolation** — app bound to loopback; `tailscale serve` (tailnet-private TLS) is the only
   ingress; never WAN, never `funnel`. (This governs the *relay*. The *host's* own WAN exposure —
   QuickConnect, DDNS, port-forwarding — is independent and not controlled here; the
   `internet_exposure` Tier-A tool reports it. The 2026-05 audit wrongly cleared QuickConnect on
   both NAS; see `docs/audit-2026-05.md`.)
2. **Access** — per-appliance bearer token (`StaticTokenVerifier`, real 401) **and** Tailscale
   ACL (your identity → the relay, tcp:443) **and** optional `ALLOWED_USERS` identity check.
3. **Ephemerality** — `restart: "no"`; brought up only for a session; idle watchdog auto-stops.
4. **Audit** — every call logged with caller identity + correlation id; logs dir `chmod 700` root.

Plus **execution friction**: `run()` requires a replayed dry-run token and refuses catastrophic
patterns outright (see below). The bearer token is the crown jewel — token + tailnet access =
root on the box.

### Host-side: scoped passwordless sudo

`install-sudo.sh` grants the relay user NOPASSWD sudo for **only** the lifecycle scripts —
root-owned copies at `/usr/local/sbin/mage-hands-relay-{up,down}` (the relay user can neither
edit the copies nor swap the root-owned directory). So starting/stopping the relay is
unattended-friendly, while every other sudo — anything genuinely destructive — still requires
the password, i.e. a human. (This scopes *direct* sudo; the relay container is privileged by
design and is gated separately by the layers above.)

### Client-side: approval gates

On the Mac, `~/.claude/settings.json` permission rules make read-only relay tools auto-run while
the side-effecting ones pause for the human:

| Permission | Tools |
|------------|-------|
| `allow` (auto-run) | `system_info`, `disk_usage`, `storage_health`, `list_containers`, `container_logs`, `service_status`, `read_file` |
| `ask` (approval each call) | `restart_container`, `restart_service`, `run`, and the `relay.sh` start/stop helper |

Claude can investigate freely; every mutation, raw exec, and relay start requires an explicit
approval. "Truly destructive" actions are thus gated three ways: the relay's own denylist +
exec-token, the Mac approval prompt, and (for non-lifecycle sudo) the NAS password.

## Tool tiers

| Tier | Nature | Examples | Gating |
|------|--------|----------|--------|
| **A** | inspection (read-only) | `system_info`, `disk_usage`, `storage_health`, `list_containers`, `container_logs`, `service_status`, `internet_exposure`, `performance`, `pending_updates`, `read_file` | none; `read_file` is allow/deny policied |
| **B** | controlled mutation | `restart_container`, `restart_service` | typed args, audited, `destructiveHint` |
| **C** | raw root exec | `run(command, exec_token)` | dry-run → one-time replay token + catastrophic-pattern denylist |

### The `run()` gate (two-call state machine)

```
run(command)                      → { dry_run: true, would_run, exec_token, ttl_seconds }
        │  (token bound to sha256(command), 5-min TTL, in-memory)
        ▼
run(command, exec_token=<token>)  → executes on host  (if token valid, unexpired, command unchanged)
                                  → { refused: true, reason } otherwise

run("rm -rf /")                   → { refused: true } at step 1 (DEFAULT_DENY), never tokenized
```

`DEFAULT_DENY` (regex, in `exec.py`) is a backstop, not a guarantee. It blocks whole-pool/root
destruction including trailing-slash and glob forms; targeted deletes under a volume are allowed
intentionally.

## Audit record schema

One JSON object per line in `<AUDIT_DIR>/audit.jsonl` (rotating, 10 MB × 10):

```json
{"ts": 1779423581.61, "cid": "7bb62eaf39fde60e", "node": "kappa",
 "user": "you@example.com", "tool": "system_info", "args": {},
 "status": "ok", "ms": 97}
```

`status` is `ok` or `error:<ExceptionType>`. A refused `run()` logs `ok` because the tool
returned a refusal payload rather than raising. `<AUDIT_DIR>/last_activity` holds the epoch of
the last call (atomic write) and drives the idle watchdog.

## Configuration (environment)

| Var | Default | Meaning |
|-----|---------|---------|
| `RELAY_TOKEN` | *(required)* | Shared bearer token; must match the Mac's `claude mcp add` header. |
| `NODE_ID` | hostname | Appliance identifier in the audit log. |
| `ALLOWED_USERS` | *(empty)* | Comma-separated Tailscale logins allowed to call tools; empty = token+ACL only. |
| `AUDIT_DIR` | `/var/log/mcp` | Where the audit log + `last_activity` are written (a mounted volume). |
| `BIND_HOST` / `PORT` / `MCP_PATH` | `0.0.0.0` / `8787` / `/mcp` | Listen address inside the container (published to host loopback only). |
| `GRACEFUL_TIMEOUT` | `30` | Seconds uvicorn drains in-flight calls on shutdown (compose `stop_grace_period` ≥ this). |
| `OUTPUT_CAP` | `65536` | Runtime cap (bytes) on every tool's stdout/stderr, incl. `run()`. Raising it fixes silent truncation of large output (the audit hit the old hard-coded 4000). |
| `OUTPUT_CAP_MAX` | `2097152` | Hard ceiling; neither `OUTPUT_CAP` nor a per-call `run(max_bytes=…)` may exceed it (anti-DoS / context-blowup). Per-call `max_bytes` only ever *narrows*. |
| `RUN_DENY_EXTRA` | *(empty)* | Comma/newline-separated extra denylist regexes, **appended** to `DEFAULT_DENY` (never replaces). Invalid regex fails at startup. |
| `READ_ALLOW_EXTRA` / `READ_DENY_EXTRA` | *(empty)* | Extra `read_file` allow/deny roots, **added** to the appliance defaults. Set-but-empty is a no-op (never wipes a default). |
| `READ_POLICY_OVERRIDE` | *(unset)* | `1` makes the `*_EXTRA` lists fully **replace** the appliance read defaults (logged loudly at startup). Default = additive. |
| `IDLE_SECONDS` | `1800` | Idle-watchdog timeout on the NAS before `relay-down.sh` (set in the Task Scheduler job, not the container). |

## Deployment shape (synology-hands)

- **Container:** `privileged`, `pid: host`, `restart: "no"`, mounts `/:/host` and `./logs`,
  publishes `127.0.0.1:8787:8787`, TCP healthcheck, `stop_grace_period: 35s`.
- **Image build context is the repo root** so the image can `COPY common` and install the core,
  then `COPY synology-hands/server.py`.
- **Ingress:** `tailscale serve --bg --https=443 http://localhost:8787`.
- **Lifecycle:** `scripts/relay-up.sh` (build → wait healthy → serve), `relay-down.sh` (serve
  off → compose down), `idle-watchdog.sh` (DSM Task Scheduler, stops after `IDLE_SECONDS`).

## Deployment shape (router-hands) — the SSHRunner variant

A second supported topology for targets that can't host the relay (ASUS Asuswrt-Merlin: BusyBox,
no Docker, no nsenter). The relay runs in a container on a NAS (`kappa`) and reaches the router
over SSH; ingress is a **Tailscale sidecar** giving it its own node `router1`.

- **Two services, one netns:** a `tailscale/tailscale` sidecar (`hostname: router1`,
  `TS_USERSPACE=true`, declarative `TS_SERVE_CONFIG=serve.json` mapping `:443 → 127.0.0.1:8788`)
  plus the relay with `network_mode: "service:tailscale"`. The relay binds `127.0.0.1:8788` inside
  that shared namespace (so `BIND_HOST=127.0.0.1`, `PORT=8788`); SSH egress to the router's LAN IP
  leaves via the sidecar's Docker bridge.
- **Unprivileged:** no `privileged`, no `pid: host`, no `/:/host` — the relay only SSHes out. The
  SSH private key is bind-mounted read-only (`./secrets/…:/secrets/router_key`), never baked in;
  the router host key is pinned (`known_hosts`).
- **Execution:** `SSHRunner` renders each tool's argv with `shlex.join` into one remote command,
  prefixed with an explicit `PATH` (dropbear strips the environment). Shell payloads (`sh -c …`)
  invoke an **absolute** shell (`/bin/sh`, override `ROUTER_REMOTE_SHELL`) because Broadcom firmware
  squats the name `sh` with a memory-diagnostic applet on `PATH` — a bare `sh -c` would hit *it*,
  not busybox (see lessons.md). `read_file` uses
  `runner_reader` (read over SSH); `run()` is **on by default** (`ROUTER_ENABLE_RUN=false` to
  disable) with a router-tuned denylist on top of `DEFAULT_DENY` — the router extras also close the
  indirect Merlin reboot paths (`service reboot`/`init 6`/…) so the gated `reboot_router` tool stays
  the only intended reboot route.
- **Ingress:** declarative via the sidecar — there is **no** `tailscale serve` CLI call.
- **Lifecycle:** `scripts/relay-up.sh` (compose up both → wait relay healthy → SSH-egress check),
  `relay-down.sh` (compose down — sidecar/node go too), `idle-watchdog.sh`, `install-sudo.sh`
  (distinct `mage-hands-router-relay-{up,down}`). See [router-hands/README.md](router-hands/README.md).
