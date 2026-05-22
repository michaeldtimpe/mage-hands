"""mage-hands :: synology-hands — MCP relay for administering a Synology NAS host.

Runs in a privileged container (pid:host) and drives the host via ``nsenter -t 1``, so it can
use the host's own toolchain (docker, smartctl, synoservicectl, synogetkeyvalue, ...). All the
security machinery — token auth, forensic audit, the gated run() tool, the read path policy —
comes from mage_hands_core; this module just registers Synology-specific tools.
"""

from __future__ import annotations

import sys

from mage_hands_core import (
    Config,
    DEFAULT_DENY,
    NsenterRunner,
    PathPolicy,
    build_server,
    fs_reader,
    register_read_file,
    register_run_tool,
    run_server,
)

INSTRUCTIONS = (
    "You operate directly on the Synology HOST namespace via this relay. File paths are "
    "host-absolute to the NAS storage pools (e.g. /volume1/...), NOT inside a container. "
    "Prefer Tier-A inspection tools. For run(): always dry-run first (call without exec_token), "
    "show the user the intended command, then execute by replaying the returned exec_token."
)

# Read policy — widen ALLOW / tighten DENY per box via READ_ALLOW_EXTRA / READ_DENY_EXTRA (or
# fully replace with READ_POLICY_OVERRIDE=1). See config.py.
READ_ALLOW = ["/volume1", "/var/log", "/etc/synoinfo.conf", "/etc/VERSION"]
READ_DENY = [
    "/etc/shadow",
    "/etc/ssh",
    "/root/.ssh",
    "/root/.gnupg",
    "/var/packages/Tailscale/etc",
    "/volume1/@docker",   # docker secrets / socket area
]


def main() -> None:
    cfg = Config.from_env()
    host = NsenterRunner(cap=cfg.output_cap)
    mcp = build_server("synology-hands", INSTRUCTIONS, cfg)

    # ---- small host-command helpers (read-only) ----
    def out(r: dict) -> str:
        return (r.get("stdout") or "").strip()

    def getkey(conf: str, key: str) -> dict:
        """synogetkeyvalue <conf> <key> → {rc, stdout, stderr}."""
        return host.run(["synogetkeyvalue", conf, key])

    # ---- Tier A: inspection (read-only) ----
    @mcp.tool()
    def system_info() -> dict:
        """Kernel/host identity (uname -a) and DSM version."""
        return {"uname": host.run(["uname", "-a"]), "dsm": host.run(["cat", "/etc/VERSION"])}

    @mcp.tool()
    def disk_usage() -> dict:
        """Filesystem usage (df -h)."""
        return host.run(["df", "-h"])

    @mcp.tool()
    def storage_health() -> dict:
        """SMART health for every detected disk: scan, then `-H` per device."""
        scan = host.run(["smartctl", "--scan"])
        devices = [
            line.split()[0]
            for line in (scan.get("stdout") or "").splitlines()
            if line.startswith("/dev/")
        ]
        return {"devices": {dev: host.run(["smartctl", "-H", dev]) for dev in devices}}

    @mcp.tool()
    def list_containers() -> dict:
        """All Docker / Container Manager containers (docker ps -a)."""
        return host.run(["docker", "ps", "-a", "--format", "{{json .}}"])

    @mcp.tool()
    def container_logs(name: str, tail: int = 100) -> dict:
        """Tail logs for a container by name."""
        return host.run(["docker", "logs", "--tail", str(tail), name])

    @mcp.tool()
    def service_status(name: str) -> dict:
        """Status of a DSM service (synoservicectl --status <name>)."""
        return host.run(["synoservicectl", "--status", name])

    @mcp.tool()
    def internet_exposure() -> dict:
        """Tier A — authoritative external-access posture: QuickConnect, DDNS, UPnP, port
        forwarding, reverse proxy.

        Each channel reports {enabled, ...detail, source, confidence} where confidence is
        'authoritative' | 'heuristic' | 'unknown'. CRITICAL: 'unknown' (no evidence either way)
        is NEVER collapsed into 'disabled' — conflating those is exactly how the 2026-05 audit
        wrongly cleared QuickConnect (it read /etc/synoinfo.conf, which has no quickconnect key).
        Raw probe outputs are included under 'signals' so the authority mapping can be refined
        from observation. Secret-ish values (DDNS creds, registration tokens) are not returned.
        """
        return {
            "quickconnect": _quickconnect(host),
            "ddns": _ddns(host, getkey),
            "upnp": _upnp(host, getkey),
            "port_forwarding": _port_forwarding(host),
            "reverse_proxy": _reverse_proxy(host),
            "note": "a configured reverse proxy / DDNS entry is not by itself proof of live WAN "
                    "ingress; relay ingress remains loopback + tailscale serve.",
        }

    @mcp.tool()
    def performance() -> dict:
        """Tier A — resource pressure snapshot with operator-grade summary fields (load relative
        to core count, swap pressure, instantaneous iowait, top processes). Missing metrics
        (e.g. CPU temp on boxes without hwmon) report 'unavailable' rather than failing the call.
        """
        return _performance(host, out)

    @mcp.tool()
    def pending_updates() -> dict:
        """Tier A — update posture in DISTINCT buckets (don't flatten): DSM OS, Package Center
        packages, externally/vendor-managed (e.g. Tailscale, which Package Center lags), and a
        pointer to container-image drift. `synopkg checkupdateall` phones home, so it runs with
        an extended timeout.
        """
        return _pending_updates(host, out)

    # ---- Tier B: controlled mutation (narrow, audited) ----
    @mcp.tool(annotations={"destructiveHint": True})
    def restart_container(name: str) -> dict:
        """Restart a single container by name."""
        return host.run(["docker", "restart", name])

    @mcp.tool(annotations={"destructiveHint": True})
    def restart_service(name: str) -> dict:
        """Restart a DSM service (synoservicectl --restart <name>)."""
        return host.run(["synoservicectl", "--restart", name])

    # ---- Tier A: policied file read (additive-first; full replace only with override) ----
    if cfg.read_policy_override:
        allow, deny = cfg.read_allow_extra, cfg.read_deny_extra
        print(
            f"[mage-hands] READ_POLICY_OVERRIDE=1 — read policy fully REPLACED. "
            f"effective allow={allow} deny={deny}",
            file=sys.stderr,
            flush=True,
        )
    else:
        allow = READ_ALLOW + cfg.read_allow_extra
        deny = READ_DENY + cfg.read_deny_extra
    register_read_file(mcp, PathPolicy(allow, deny), fs_reader("/host"))

    # ---- Tier C: gated raw exec (denylist = DEFAULT_DENY + additive RUN_DENY_EXTRA) ----
    register_run_tool(
        mcp,
        host,
        deny_patterns=DEFAULT_DENY + cfg.run_deny_extra,   # additive only — never replaces
        output_cap=cfg.output_cap,
    )

    run_server(mcp, cfg)


# ── internet_exposure channel probes ──────────────────────────────────────────────────────────
# Live, stateless, ranked probing per call (no shared mutable lock → no race). Returns a verdict
# with provenance; "unknown" is distinct from "disabled".

def _quickconnect(host) -> dict:
    """Authoritative source = the QuickConnect relay daemon's own config + whether it's running.

    DSM stores QuickConnect state in /usr/syno/etc/synorelayd/synorelayd.conf (JSON), NOT in
    /etc/synoinfo.conf and NOT in a synoinfo_quickconnect.conf — and synogetkeyvalue on a
    missing file returns rc 0 + empty, which is exactly how the 2026-05 audit false-negatived it.
    So we read the daemon config directly and corroborate with the running synorelayd process.
    """
    import json
    signals: dict = {}

    daemon = host.run(["sh", "-c", "ps -eo pid,comm 2>/dev/null | grep -iE 'synorelayd|relayd' | grep -v grep"])
    running = bool((daemon.get("stdout") or "").strip())
    signals["synorelayd_running"] = running

    conf = host.run(["cat", "/usr/syno/etc/synorelayd/synorelayd.conf"])
    body = (conf.get("stdout") or "").strip()
    if conf.get("rc") == 0 and body.startswith("{"):
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, dict):
            enabled = bool(data.get("quickconnect", {}).get("enabled"))
            services = [s.get("id") for s in data.get("service", []) if isinstance(s, dict)]
            return {
                "enabled": enabled,
                "id": (data.get("server_alias", {}) or {}).get("alias") or data.get("serverID"),
                "relayed_services": services,   # NB: 'ssh'/'dsm' here = reachable via the QC relay
                "smartdns": bool((data.get("server_smartdns", {}) or {}).get("enabled")),
                "daemon_running": running,
                "source": "/usr/syno/etc/synorelayd/synorelayd.conf",
                "confidence": "authoritative",
            }

    missing = "no such file" in ((conf.get("stderr") or "") + body).lower()
    if missing and not running:
        # the daemon's source-of-truth config is gone and nothing is relaying → off.
        return {"enabled": False, "daemon_running": False,
                "source": "/usr/syno/etc/synorelayd/synorelayd.conf (absent) + no synorelayd",
                "confidence": "authoritative", "signals": signals}
    if running:
        return {"enabled": True, "daemon_running": True,
                "source": "running synorelayd daemon (config unparsed)",
                "confidence": "heuristic", "signals": signals}
    return {"enabled": None, "source": None, "confidence": "unknown",
            "note": "synorelayd config unreadable and daemon down; absence of evidence is NOT "
                    "'disabled'",
            "signals": signals}


def _ddns(host, getkey) -> dict:
    upd = getkey("/etc/synoinfo.conf", "ddns_update")
    sel = getkey("/etc/synoinfo.conf", "ddns_select")
    u, s = (upd.get("stdout") or "").strip(), (sel.get("stdout") or "").strip()
    if upd.get("rc") == 0:
        return {"enabled": u == "yes", "provider": s or None,
                "source": "/etc/synoinfo.conf:ddns_update", "confidence": "authoritative"}
    return {"enabled": None, "confidence": "unknown",
            "signals": {"ddns_update": u, "ddns_select": s}}


def _upnp(host, getkey) -> dict:
    r = getkey("/etc/synoinfo.conf", "runupnp")
    v = (r.get("stdout") or "").strip()
    if r.get("rc") == 0:
        return {"enabled": v == "yes", "source": "/etc/synoinfo.conf:runupnp",
                "confidence": "authoritative"}
    return {"enabled": None, "confidence": "unknown", "signals": {"runupnp": v}}


def _port_forwarding(host) -> dict:
    # DSM router port-forward rules live under /usr/syno/etc/portforward* when configured.
    ls = host.run(["sh", "-c", "ls /usr/syno/etc/portforward* 2>/dev/null"])
    listing = (ls.get("stdout") or "").strip()
    if not listing:
        return {"rules": 0, "source": "no /usr/syno/etc/portforward* files",
                "confidence": "heuristic"}
    return {"present": True, "source": listing[:500], "confidence": "heuristic",
            "note": "rule files present; inspect contents to enumerate"}


def _reverse_proxy(host) -> dict:
    cat = host.run(["cat", "/usr/syno/etc/www/ReverseProxy.json"])
    body = (cat.get("stdout") or "").strip()
    if cat.get("rc") != 0 or not body:
        return {"entries": 0, "source": "/usr/syno/etc/www/ReverseProxy.json (absent/empty)",
                "confidence": "authoritative"}
    import json
    try:
        data = json.loads(body)
        entries = data if isinstance(data, list) else data.get("entries", [])
        n = len(entries) if isinstance(entries, list) else 0
    except Exception:
        n = None
    return {"entries": n, "source": "/usr/syno/etc/www/ReverseProxy.json",
            "confidence": "authoritative",
            "note": "a reverse-proxy definition is not by itself public WAN ingress"}


# ── performance ───────────────────────────────────────────────────────────────────────────────

def _performance(host, out) -> dict:
    res: dict = {}

    ncpu = 1
    nr = host.run(["nproc"])
    try:
        ncpu = max(1, int(out(nr)))
    except (TypeError, ValueError):
        pass
    res["cpu_count"] = ncpu

    # load
    la = out(host.run(["cat", "/proc/loadavg"])).split()
    if len(la) >= 3:
        try:
            l1, l5, l15 = float(la[0]), float(la[1]), float(la[2])
            per_core = round(l1 / ncpu, 2)
            res["load"] = {"1m": l1, "5m": l5, "15m": l15, "per_core_1m": per_core,
                           "state": "healthy" if per_core < 0.7 else
                                    "elevated" if per_core < 1.0 else "high"}
        except ValueError:
            res["load"] = "unavailable"
    else:
        res["load"] = "unavailable"

    # memory + swap
    mem = {}
    for line in out(host.run(["cat", "/proc/meminfo"])).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in (
            "MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"
        ):
            try:
                mem[parts[0].rstrip(":")] = int(parts[1])  # kB
            except ValueError:
                pass
    if mem.get("MemTotal"):
        used_pct = round(100 * (1 - mem.get("MemAvailable", mem.get("MemFree", 0)) / mem["MemTotal"]), 1)
        sw_total = mem.get("SwapTotal", 0)
        sw_used = sw_total - mem.get("SwapFree", 0)
        sw_pct = round(100 * sw_used / sw_total, 1) if sw_total else 0.0
        res["memory"] = {
            "total_mib": round(mem["MemTotal"] / 1024),
            "available_mib": round(mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024),
            "used_pct": used_pct,
            "swap_used_mib": round(sw_used / 1024),
            "swap_pressure": "none" if sw_used == 0 else "low" if sw_pct < 10 else
                             "moderate" if sw_pct < 50 else "high",
        }
    else:
        res["memory"] = "unavailable"

    # instantaneous iowait via two /proc/stat samples ~1s apart
    stat = host.run(["sh", "-c", "cat /proc/stat; sleep 1; echo ---; cat /proc/stat"])
    res["iowait_pct"] = _iowait_delta(stat.get("stdout") or "")

    # uptime
    res["uptime"] = out(host.run(["uptime"])) or "unavailable"

    # top processes (ps with sort; fall back to top)
    ps = host.run(["sh", "-c", "ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu 2>/dev/null | head -n 8"])
    if ps.get("rc") == 0 and out(ps):
        res["top_processes"] = out(ps)
    else:
        top = host.run(["sh", "-c", "top -bn1 2>/dev/null | head -n 15"])
        res["top_processes"] = out(top) or "unavailable"

    # CPU temperature (best-effort; many boxes lack hwmon)
    res["cpu_temp_c"] = _cpu_temp(host)
    return res


def _iowait_delta(text: str) -> object:
    chunks = text.split("---")
    if len(chunks) != 2:
        return "unavailable"

    def cpu_fields(chunk: str):
        for line in chunk.splitlines():
            if line.startswith("cpu "):
                return [int(x) for x in line.split()[1:]]
        return None

    a, b = cpu_fields(chunks[0]), cpu_fields(chunks[1])
    if not a or not b or len(a) < 5 or len(b) < 5:
        return "unavailable"
    total = sum(b) - sum(a)
    iowait = b[4] - a[4]
    if total <= 0:
        return 0.0
    return round(100 * iowait / total, 1)


def _cpu_temp(host):
    cmd = (
        "for f in /sys/class/hwmon/hwmon*/temp*_input /sys/class/thermal/thermal_zone*/temp; do "
        "[ -r \"$f\" ] && cat \"$f\"; done 2>/dev/null"
    )
    r = host.run(["sh", "-c", cmd])
    vals = []
    for line in (r.get("stdout") or "").split():
        try:
            v = int(line)
            vals.append(round(v / 1000) if v > 1000 else v)
        except ValueError:
            pass
    if not vals:
        return "unavailable"
    return {"max": max(vals), "readings": vals}


# ── pending_updates ─────────────────────────────────────────────────────────────────────────

def _pending_updates(host, out) -> dict:
    res: dict = {}

    # DSM OS — `synoupgrade --check` (on PATH, NOT /usr/syno/bin) returns a status TOKEN, not
    # prose: e.g. UPGRADE_NEWEST (current), UPGRADE_HAVENEW/_READY (update staged/available),
    # UPGRADE_CHECKNEWDSM (inconclusive). Map the known ones; include current version (from
    # /etc/VERSION) so the consumer can compare against the fleet / release notes.
    cur = ""
    for line in out(host.run(["cat", "/etc/VERSION"])).splitlines():
        if line.startswith("productversion="):
            cur = line.split("=", 1)[1].strip().strip('"')
    dsm = host.run(["synoupgrade", "--check"], timeout=120)
    token = out(dsm).upper()
    if any(k in token for k in ("HAVENEW", "READY", "AVAILABLE", "DOWNLOAD")):
        available = True
    elif any(k in token for k in ("NEWEST", "NOUPDATE", "UPTODATE")):
        available = False
    else:
        available = None   # token not conclusive (e.g. CHECKNEWDSM) — confirm in DSM UI
    res["dsm_os"] = {
        "current_version": cur or "unknown",
        "check_status": out(dsm)[:200],
        "rc": dsm.get("rc"),
        "available": available,
        "source": "synoupgrade --check + /etc/VERSION",
    }

    # Package Center (phones home → extended timeout)
    pkg = host.run(["synopkg", "checkupdateall"], timeout=180)
    res["packages"] = {
        "check_raw": out(pkg)[:1500],
        "source": "synopkg checkupdateall",
        "note": "Package Center can lag upstream for months (see vendor_managed)",
    }
    lst = host.run(["sh", "-c", "synopkg list --name 2>/dev/null || synopkg list 2>/dev/null"])
    res["packages"]["installed"] = out(lst)[:3000]

    # Externally/vendor-managed (Package Center is unreliable for these)
    tv = host.run(["tailscale", "version"])
    tc = host.run(["tailscale", "update", "--dry-run"], timeout=60)  # --check is not a flag
    tc_text = (out(tc) + " " + (tc.get("stderr") or "").strip()).strip()
    res["vendor_managed"] = {
        "tailscale": {
            "version": out(tv).splitlines()[0] if out(tv) else "unavailable",
            "update_dry_run": tc_text[:500] or "unavailable",
            "source": "tailscale version / tailscale update --dry-run",
            "note": "update via `tailscale update --yes`, NOT Package Center",
        }
    }

    res["container_images"] = {
        "note": "image drift is not auto-computed; use list_containers + `docker images` to "
                "compare tags/ages.",
    }
    return res


if __name__ == "__main__":
    main()
