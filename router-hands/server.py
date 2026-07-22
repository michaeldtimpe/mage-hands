"""mage-hands :: router-hands — MCP relay for administering an ASUS Asuswrt-Merlin router.

Unlike synology-hands (a privileged container driving its OWN host via nsenter), this relay runs
in an UNprivileged container on the NAS and reaches the router over SSH (``SSHRunner``). The
router has no Docker and no nsenter; it only needs SSH enabled + the relay's public key. All the
security machinery — token auth, forensic audit, the gated run() tool, the read path policy —
comes from mage_hands_core; this module just registers router-specific tools.

Trust-model note: because reads happen over SSH, ``read_file`` is best-effort constrained reading
on a *trusted* appliance (PathPolicy is lexical and can't resolve remote symlinks), and the
denylist regexes on run() are guardrails, not a sandbox. See runner_reader() and exec.py.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from typing import Annotated, Literal, get_args

from pydantic import Field

from mage_hands_core import (
    Config,
    DEFAULT_DENY,
    PathPolicy,
    SSHRunner,
    build_server,
    register_read_file,
    register_run_tool,
    run_server,
    runner_reader,
)

INSTRUCTIONS = (
    "You operate on an ASUS Asuswrt-Merlin router (BusyBox userland) over SSH via this relay. "
    "File paths are ROUTER-absolute (e.g. /jffs/..., /tmp/..., /proc/net/...), NOT a NAS or "
    "container path. The router has no Docker and no systemd; services are controlled with "
    "Merlin's `service restart_<name>`. Prefer the Tier-A inspection tools (system_info, "
    "diagnostics, clients, dhcp_leases, wan_status, interfaces, firewall_show, disk_usage, "
    "performance, pending_updates, internet_exposure). `reboot_router` is the only sanctioned "
    "reboot path and is approval- AND confirm-gated. For run() (present by default; disable with "
    "ROUTER_ENABLE_RUN=false): always dry-run first (call without exec_token), show the user the "
    "intended command, then execute by replaying the returned exec_token; avoid firmware/"
    "nvram-erase operations. Some operations (WAN/firewall/wireless restarts, reboot) briefly "
    "drop the SSH transport — a `transport_error` or timeout result is INDETERMINATE, not "
    "necessarily a failure; re-inspect to confirm what actually happened."
)

# Read policy — widen ALLOW / tighten DENY per box via READ_ALLOW_EXTRA / READ_DENY_EXTRA (or
# fully replace with READ_POLICY_OVERRIDE=1). See config.py. /var and /tmp are world-writable and
# the highest remote-symlink risk, so the DENY list explicitly blocks every known secret trap.
READ_ALLOW = ["/jffs", "/tmp", "/var", "/etc", "/proc/net", "/proc/meminfo", "/proc/loadavg",
              "/proc/uptime"]
READ_DENY = [
    "/etc/shadow",
    "/etc/ssh",
    "/etc/dropbear",          # dropbear SSH host private keys
    "/root/.ssh",
    "/jffs/.sys",             # Merlin internal/system state
    "/jffs/.cert",            # web-UI TLS keys/certs
    "/jffs/.le",              # Let's Encrypt material
    "/jffs/openvpn",          # VPN keys/certs
    "/etc/openvpn",
    "/etc/wireguard",
    "/tmp/etc/openvpn",
    "/tmp/etc/wireguard",
    "/var/tmp",               # world-writable symlink trap
    "/tmp/var",
]

# Router-specific catastrophic patterns, APPENDED to the core DEFAULT_DENY (which already blocks
# rm -rf /, mkfs, dd of=/dev/*, fdisk/parted, recursive chmod/chown on /, and the availability
# backstops reboot/shutdown/poweroff/halt/init 0/kill -1). These add the soft-brick cases unique
# to a router: jffs/USB wipes, mtd/flash writes, factory reset, and boot-state tampering.
#
# Indirect-reboot closure (matters now that run() is ON by default): DEFAULT_DENY anchors
# reboot/shutdown/etc. to *command position*, so several Merlin-valid reboot triggers slip through
# (verified empirically): `service reboot`, `init 6`/`telinit 6`, `busybox reboot`, `rc reboot`,
# `killall rc`. We deny those here so `reboot_router` stays the only DIRECTLY-intended reboot path.
# NOTE: the denylist is a lexical backstop, not a guarantee — string-wrapped forms like
# `sh -c reboot` / `echo reboot | sh` remain evadable; real safety is the dry-run/token gate +
# audit + ephemerality + human approval.
ROUTER_DENY_EXTRA = [
    r"rm\s+-[a-z]*r[a-z]*f?\s+/jffs/?\*?(?:\s|$)",     # wipe the persistent store
    r"rm\s+-[a-z]*r[a-z]*f?\s+/tmp/mnt(?:\s|/|$)",     # wipe mounted USB/addon storage
    r"\bmtd[-_](?:write|erase)\b",                     # mtd-write / mtd_erase firmware flashing
    r"\bflash_erase(?:all)?\b",
    r"\bnandwrite\b",
    r"\bfwupg\b",
    r"\bnvram_upgrade\b",
    r"\bnvram\s+(?:erase|restore|restore-defaults|fb)\b",   # factory reset
    r"\bsysstate\b",                                   # boot/runtime state tampering
    r">\s*/dev/(?:mtd|mmcblk)",                        # DEFAULT_DENY only covers > /dev/sd
    r"\bmke2fs\b",
    r"\bformat\b",
    # indirect-reboot paths that bypass DEFAULT_DENY's command-position anchor
    r"\bservice\s+reboot\b",
    r"\bservice\s+restart_reboot\b",
    r"\b(?:tel)?init\s+6\b",
    r"\bbusybox\s+reboot\b",
    r"\brc\s+reboot\b",
    r"\bkillall\s+rc\b",
]

# Tier-B service allowlist: name → fixed verb `service restart_<name>`. Keeps the tool from being
# used for arbitrary `service` verbs (start_*/stop_*/reboot/firmware helpers). Declared as a
# Literal so the fixed set lands in the tool schema as an enum (the model sees the valid values
# up front instead of discovering them from a refusal); ALLOWED_SERVICES is derived from it and
# stays the runtime backstop.
ServiceName = Literal[
    "dnsmasq", "wireless", "firewall", "wan", "httpd", "net", "samba", "nasapps",
    "vpnclient1", "vpnclient2", "vpnclient3", "vpnclient4", "vpnclient5",
    "vpnserver1", "vpnserver2", "wgc", "wgs",
]
ALLOWED_SERVICES = frozenset(get_args(ServiceName))

# internet_exposure() reads ONLY these nvram keys, batched into one ssh round-trip. This frozen
# allowlist IS the security boundary: we never run `nvram show` (it would dump http_passwd,
# wl*_wpa_psk, ddns_passwd, vpn keys/certs, ...). Keep it a constant — never build it dynamically
# or from input. Every key here is a boolean/port/provider/public-name; none is a credential.
_EXPOSURE_NVRAM_KEYS = (
    "misc_http_x", "misc_httpport_x", "misc_httpsport_x", "http_enable",     # WAN web admin
    "sshd_enable", "sshd_wan", "sshd_port",                                   # SSH
    "telnetd_enable",                                                         # telnet
    "vts_enable_x", "vts_rulelist",                                           # port forwards
    "upnp_enable",                                                            # UPnP/NAT-PMP
    "dmz_ip",                                                                 # DMZ host
    "ddns_enable_x", "ddns_server_x", "ddns_hostname_x",                      # DDNS (public name)
    "enable_webdav", "webdav_aidisk", "webdav_smartaccess",                   # AiCloud/WebDAV
    "vpn_server_enable", "vpn_server1_state", "vpn_server2_state",            # OpenVPN server
    "vpn_server_port", "vpn_server_proto", "wgs_enable", "wgs_port",          # OpenVPN/WireGuard
    "ipv6_fw_enable", "ipv6_fw_rulelist", "ipv6_service",                     # IPv6 firewall
    "enable_ftp", "ftp_wanac", "st_ftp_mode",                                 # FTP over WAN
    "autofw_enable", "autofw_rulelist",                                       # port triggering
)

# Defense-in-depth: fail at IMPORT time (before any tool registers) if a secret-bearing key ever
# sneaks into the exposure allowlist. Catches http_passwd / *_wpa_psk / ddns_passwd / vpn_crt_*_ca /
# *_key / wgs_priv etc. A misconfigured deploy crashes loudly rather than leaking a credential.
_SECRET_KEY_RE = re.compile(r"passwd|psk|wpa|secret|priv|crt|_ca|key")
_FORBIDDEN_EXPOSURE_KEYS = [k for k in _EXPOSURE_NVRAM_KEYS if _SECRET_KEY_RE.search(k)]
assert not _FORBIDDEN_EXPOSURE_KEYS, (
    "internet_exposure nvram allowlist contains secret-bearing key(s): "
    f"{_FORBIDDEN_EXPOSURE_KEYS} — refusing to start (would risk leaking credentials)."
)


def _discover_wifi_ifaces(host) -> list[str]:
    """WiFi interface names from nvram (wl_ifnames + lan_ifnames), de-duplicated. Names vary by
    model/chipset (eth1/eth2 on newer tri-band, wl0/wl1 on older), so we never hardcode them."""
    seen: list[str] = []
    for key in ("wl_ifnames", "lan_ifnames"):
        for tok in (host.run(["nvram", "get", key]).get("stdout") or "").split():
            if tok.startswith(("wl", "eth")) and tok not in seen:
                seen.append(tok)
    return seen


def main() -> None:
    cfg = Config.from_env()
    host = SSHRunner.from_env(cap=cfg.output_cap)
    mcp = build_server("router-hands", INSTRUCTIONS, cfg)

    # Optional static override of the probed WiFi interface list.
    wifi_override = [t for t in re.split(r"[\s,]+", os.environ.get("ROUTER_WIFI_IFACES", "")) if t]

    def out(r: dict) -> str:
        return (r.get("stdout") or "").strip()

    # ---- Tier A: inspection (read-only) ----
    @mcp.tool()
    def system_info() -> dict:
        """Router identity: kernel (uname -a), Merlin firmware (build/version), uptime, load, memory."""
        return {
            "uname": host.run(["uname", "-a"]),
            "model": host.run(["nvram", "get", "productid"]),
            "buildno": host.run(["nvram", "get", "buildno"]),
            "firmver": host.run(["nvram", "get", "firmver"]),
            "extendno": host.run(["nvram", "get", "extendno"]),
            "uptime": host.run(["uptime"]),
            "loadavg": host.run(["cat", "/proc/loadavg"]),
            "memory": host.run(["free"]),
        }

    @mcp.tool()
    def diagnostics() -> dict:
        """Tier A — SSH transport self-test. Round-trips a cheap `nvram get model` and reports
        latency, rc, and the transport_error flag (set when ssh ITSELF fails, rc 255) so SSH/key/
        route/PATH problems are diagnosable without guessing. Tailnet identity is read host-side
        (`docker exec router-hands-ts tailscale status`), not from here."""
        t0 = time.time()
        probe = host.run(["nvram", "get", "model"])
        return {
            "reachable": probe.get("rc") == 0 and not probe.get("transport_error"),
            "transport_error": probe.get("transport_error", False),
            "rc": probe.get("rc"),
            "model": out(probe) or None,
            "round_trip_ms": round((time.time() - t0) * 1000),
            "stderr": (probe.get("stderr") or "").strip() or None,
        }

    @mcp.tool()
    def clients() -> dict:
        """Tier A — connected clients and associated WiFi stations.

        Primary source is Asus networkmap's /tmp/clientlist.json (host/IP/MAC/iface); the ARP
        table gives L3 neighbors; per-radio `wl -i <iface> assoclist` lists associated WiFi MACs.
        WiFi ifaces are discovered from nvram unless ROUTER_WIFI_IFACES overrides; a missing iface
        returns nonzero rc and is ignored (never fails the call)."""
        ifaces = wifi_override or _discover_wifi_ifaces(host)
        return {
            "clientlist_json": host.run(["cat", "/tmp/clientlist.json"]),
            "arp": host.run(["cat", "/proc/net/arp"]),
            "wifi_ifaces": ifaces,
            "wifi_assoc": {i: host.run(["wl", "-i", i, "assoclist"]) for i in ifaces},
        }

    @mcp.tool()
    def dhcp_leases() -> dict:
        """Tier A — active DHCP leases (dnsmasq), standard path with /tmp fallback."""
        primary = host.run(["cat", "/var/lib/misc/dnsmasq.leases"])
        if primary.get("rc") == 0:
            return {"source": "/var/lib/misc/dnsmasq.leases", "leases": primary}
        return {"source": "/tmp/dnsmasq.leases", "leases": host.run(["cat", "/tmp/dnsmasq.leases"])}

    @mcp.tool()
    def wan_status() -> dict:
        """Tier A — WAN state/IP/gateway/proto/DNS for wan0 and wan1 (dual-WAN). A secondary WAN
        that isn't configured reports state but an empty ipaddr (configured=false)."""
        def wan(n: int) -> dict:
            d = {k: out(host.run(["nvram", "get", f"wan{n}_{k}"]))
                 for k in ("state_t", "ipaddr", "gateway", "proto", "dns")}
            d["configured"] = bool(d.get("ipaddr"))
            return d
        return {"wan0": wan(0), "wan1": wan(1)}

    @mcp.tool()
    def interfaces() -> dict:
        """Tier A — interface config + per-interface byte/packet counters (throughput baseline)."""
        return {
            "ifconfig": host.run(["ifconfig"]),
            "ip_addr": host.run(["ip", "-s", "addr"]),     # absent on some builds → nonzero rc
            "net_dev": host.run(["cat", "/proc/net/dev"]),
        }

    @mcp.tool()
    def firewall_show() -> dict:
        """Tier A — read-only firewall view: filter table + NAT table (iptables -L -n -v)."""
        return {
            "filter": host.run(["iptables", "-L", "-n", "-v"]),
            "nat": host.run(["iptables", "-t", "nat", "-L", "-n", "-v"]),
        }

    @mcp.tool()
    def disk_usage() -> dict:
        """Tier A — filesystem usage (df -h) across jffs/tmpfs/USB, jffs inode pressure (df -i),
        and the mount table. `/` near-100% is normal (squashfs); the mount that matters is /jffs."""
        return {
            "df": host.run(["df", "-h"]),
            "jffs_inodes": host.run(["df", "-i", "/jffs"]),
            "mounts": host.run(["cat", "/proc/mounts"]),
        }

    @mcp.tool()
    def performance() -> dict:
        """Tier A — resource snapshot: cpu_count, load (per-core), memory+swap, instantaneous
        iowait (two /proc/stat samples), uptime, top processes, thermal (CPU + per-radio WiFi,
        provenance preserved), and conntrack pressure. Missing metrics report 'unavailable'/
        'disabled' rather than failing the call."""
        ifaces = wifi_override or _discover_wifi_ifaces(host)
        return _performance(host, out, ifaces)

    @mcp.tool()
    def pending_updates(
        check: Annotated[bool, Field(
            description="true = run the ASUS update-check helper first (phones home, ~30-120s) "
                        "then re-read; false (default) = report last-known nvram state only."
        )] = False,
    ) -> dict:
        """Tier A — Merlin firmware update posture from nvram webs_state_* (last-known check) +
        current firmware + AiProtection signatures. check=True runs /usr/sbin/webs_update.sh first
        (phones home to ASUS, slow) then re-reads. Empty nvram = 'unknown', NEVER 'up to date';
        'available: false' = no newer ASUS stock fw (Merlin updates are out-of-band/manual)."""
        return _pending_updates(host, out, check)

    @mcp.tool()
    def internet_exposure() -> dict:
        """Tier A — full WAN attack-surface: remote admin, SSH/telnet, port forwards, UPnP
        (+active mappings), DMZ, DDNS, AiCloud/WebDAV, VPN servers, IPv6 firewall, FTP/Samba over
        WAN, port triggering, and live wildcard listeners. Per-channel {enabled, confidence};
        'unknown' is NEVER collapsed to 'disabled'; secrets are never returned (nvram read via a
        fixed safe-key allowlist). Configured intent != live WAN ingress."""
        return _internet_exposure(host, out)

    # ---- Tier B: controlled mutation (narrow, audited) ----
    @mcp.tool(annotations={"destructiveHint": True})
    def restart_service(
        name: Annotated[ServiceName, Field(
            description="Router service to restart, e.g. 'dnsmasq'. Only the enumerated vetted "
                        "services are accepted."
        )],
    ) -> dict:
        """Tier B — restart a router service via Merlin `service restart_<name>` (verb is fixed to
        restart_<name>, NOT `<name> restart`). Restricted to a vetted allowlist so the tool can't
        invoke arbitrary `service` verbs. Restarting wan/wireless/firewall briefly drops
        connectivity (the SSH transport may blip → an indeterminate result; re-inspect).
        Returns {rc, stdout, stderr}."""
        if name not in ALLOWED_SERVICES:
            return {"refused": True, "parameter": "name",
                    "reason": f"'name' {name!r} is not an allowed service",
                    "allowed": sorted(ALLOWED_SERVICES)}
        return host.run(["service", f"restart_{name}"])

    @mcp.tool(annotations={"destructiveHint": True})
    def reboot_router(
        confirm: Annotated[bool, Field(
            description="Must be explicitly true to reboot. false (default) = refuse and "
                        "describe what would happen."
        )] = False,
    ) -> dict:
        """Reboot the router (Merlin `reboot`, orderly — commits nvram/syncs jffs). DESTRUCTIVE:
        drops ALL connectivity for ~1-2 min. Requires confirm=True. The SSH transport WILL drop, so
        a transport_error/timeout result here is EXPECTED success, NOT a failure — re-check
        system_info in ~2 min. This is the only DIRECTLY-intended reboot path (run() denies the
        indirect ones: service reboot, init 6, busybox reboot, rc reboot, killall rc)."""
        if not confirm:
            return {"refused": True, "parameter": "confirm", "would_run": "reboot",
                    "reason": "set confirm=true to reboot; SSH WILL drop and the router is down ~1-2 min"}
        try:
            r = host.run(["reboot"], timeout=22)
        except subprocess.TimeoutExpired:
            return {"initiated": True, "transport": "timeout (expected)",
                    "advice": "router rebooting; re-check system_info in ~2 min"}
        # reboot severs SSH → transport_error (rc 255) is the expected, successful path.
        if r.get("transport_error") or r.get("rc") in (0, 255, None):
            return {"initiated": True, "transport_error": r.get("transport_error", False),
                    "rc": r.get("rc"),
                    "advice": "router rebooting; re-check system_info in ~2 min"}
        return {"initiated": False, "rc": r.get("rc"),
                "stderr": (r.get("stderr") or "").strip() or None}

    # ---- Tier A: policied file read over SSH (additive-first; full replace only with override) ----
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
    register_read_file(mcp, PathPolicy(allow, deny), runner_reader(host))

    # ---- Tier C: gated raw exec — ON by default (parity with synology-hands) ----
    # Arbitrary root on a soft-brickable target is a real blast radius, so this is the appliance's
    # most security-sensitive surface: token possession now effectively grants constrained root
    # shell on the router host. It stays behind the dry-run/exec_token gate + the denylist
    # (DEFAULT_DENY + ROUTER_DENY_EXTRA + the operator's additive RUN_DENY_EXTRA). Set
    # ROUTER_ENABLE_RUN=false to turn it off where that posture is unacceptable.
    if os.environ.get("ROUTER_ENABLE_RUN", "true").lower() not in ("0", "false", "no"):
        register_run_tool(
            mcp,
            host,
            deny_patterns=DEFAULT_DENY + ROUTER_DENY_EXTRA + cfg.run_deny_extra,
            output_cap=cfg.output_cap,
        )
    else:
        print(
            "[mage-hands] run() explicitly disabled via ROUTER_ENABLE_RUN=false.",
            file=sys.stderr,
            flush=True,
        )

    run_server(mcp, cfg)


# ── shared helpers ──────────────────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s or "")


def _nvram_many(host, keys) -> dict:
    """Read many nvram keys in ONE ssh round-trip via a server-side loop over an explicit key
    allowlist. NEVER `nvram show` (it dumps secrets) — the allowlist is the boundary. Returns
    {key: value}; value is "" for empty OR missing (nvram get on a missing key = rc 0 + empty,
    so callers must treat empty as 'unknown', never 'disabled')."""
    # shlex.quote each key: today they come only from frozen module-level tuples, but this keeps
    # a future caller with tainted keys from turning the loop into an injection point.
    script = "for k in %s; do echo \"$k=$(nvram get $k)\"; done" % " ".join(
        shlex.quote(k) for k in keys
    )
    r = host.run(["sh", "-c", script])
    vals = {k: "" for k in keys}
    for line in (r.get("stdout") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k in vals:
                vals[k] = v.strip()
    return vals


def _flag(val, true_vals=("1",)):
    """Map an nvram flag to (enabled, confidence). Empty = (None, 'unknown') — NEVER 'disabled'."""
    v = (val or "").strip()
    if v == "":
        return None, "unknown"
    return (v in true_vals), "direct_config"


# ── performance ────────────────────────────────────────────────────────────────────────────────

def _performance(host, out, ifaces) -> dict:
    res: dict = {}

    # cpu count — BusyBox may lack nproc; fall back to /proc/cpuinfo
    ncpu = 1
    try:
        ncpu = max(1, int(out(host.run(["nproc"]))))
    except (TypeError, ValueError):
        try:
            ncpu = max(1, int(out(host.run(["sh", "-c", "grep -c ^processor /proc/cpuinfo"]))))
        except (TypeError, ValueError):
            ncpu = 1
    res["cpu_count"] = ncpu

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
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_pct = round(100 * (1 - avail / mem["MemTotal"]), 1)
        sw_total = mem.get("SwapTotal", 0)
        sw_used = sw_total - mem.get("SwapFree", 0)
        sw_pct = round(100 * sw_used / sw_total, 1) if sw_total else 0.0
        res["memory"] = {
            "total_mib": round(mem["MemTotal"] / 1024),
            "available_mib": round(avail / 1024),
            "used_pct": used_pct,
            "swap_used_mib": round(sw_used / 1024),
            "swap_pressure": "none" if sw_used == 0 else "low" if sw_pct < 10 else
                             "moderate" if sw_pct < 50 else "high",
        }
    else:
        res["memory"] = "unavailable"

    # instantaneous iowait via two /proc/stat samples ~1s apart — the trustworthy CPU signal
    # (BusyBox top's header is unreliable). See _iowait_delta.
    stat = host.run(["sh", "-c", "cat /proc/stat; sleep 1; echo ---; cat /proc/stat"])
    res["iowait_pct"] = _iowait_delta(stat.get("stdout") or "")

    res["uptime"] = out(host.run(["uptime"])) or "unavailable"

    # top processes — BusyBox `ps` has no -eo/--sort, so use `top -bn1`; return RAW lines only
    # (column order/labels vary across builds — do not parse structured fields out of this).
    top = host.run(["sh", "-c", "top -bn1 2>/dev/null | head -n 15"])
    res["top_processes"] = _strip_ansi(out(top)) or "unavailable"

    res["thermal"] = _thermal(host, ifaces)
    res["conntrack"] = _conntrack(host)
    return res


def _iowait_delta(text: str) -> object:
    # Kept in sync with synology-hands/server.py:_iowait_delta — INTENTIONAL duplication: each
    # appliance owns its tool helpers (not hoisted to common/ to avoid touching the deployed NAS
    # relay). /proc/stat is identical on both Linux hosts, so the logic is the same.
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
    # counters can wrap/reset between samples → clamp to [0, 100]
    return min(100.0, max(0.0, round(100 * iowait / total, 1)))


def _thermal(host, ifaces) -> dict:
    """CPU + per-radio WiFi temps with provenance preserved (do NOT collapse into one field).
    On Broadcom/ARM the CPU temp lives in /proc/dmu/temperature ("CPU temperature : NN", already
    °C), not /sys/class/thermal. `wl phy_tempsense` errors if a radio is administratively down →
    that radio maps to 'disabled', never failing the whole block."""
    res: dict = {"cpu": "unavailable", "wifi": {}}
    cmd = (
        "if [ -r /proc/dmu/temperature ]; then echo \"dmu:$(cat /proc/dmu/temperature)\"; fi; "
        "for f in /sys/class/thermal/thermal_zone*/temp /sys/class/hwmon/hwmon*/temp*_input; do "
        "[ -r \"$f\" ] && echo \"sys:$(cat \"$f\")\"; done 2>/dev/null"
    )
    for line in (host.run(["sh", "-c", cmd]).get("stdout") or "").splitlines():
        line = line.strip()
        if res["cpu"] != "unavailable":
            break
        m = re.search(r"(\d+)", line)
        if not m:
            continue
        if line.startswith("dmu:"):
            res["cpu"] = {"value": int(m.group(1)), "source": "/proc/dmu/temperature"}
        elif line.startswith("sys:"):
            v = int(m.group(1))
            res["cpu"] = {"value": round(v / 1000) if v > 1000 else v,
                          "source": "/sys/class/thermal|hwmon"}

    for i in ifaces:
        t = host.run(["wl", "-i", i, "phy_tempsense"])
        body = (t.get("stdout") or "").strip()
        if t.get("rc") == 0 and body:
            m = re.match(r"(\d+)", body)
            res["wifi"][i] = {"value": int(m.group(1))} if m else {"raw": body[:40]}
        else:
            res["wifi"][i] = "disabled"
    return res


def _conntrack(host) -> object:
    """nf_conntrack usage with a qualitative pressure band. Tries both kernel layouts (newer ARM
    nf_conntrack_* vs older Broadcom ip_conntrack_*). Both empty → 'unavailable' (the module may
    not be loaded — a meaningful absence on a router), not a bare null."""
    cmd = (
        "for p in /proc/sys/net/netfilter/nf_conntrack_count "
        "/proc/sys/net/netfilter/nf_conntrack_max "
        "/proc/sys/net/ipv4/netfilter/ip_conntrack_count "
        "/proc/sys/net/ipv4/netfilter/ip_conntrack_max; do "
        "[ -r \"$p\" ] && echo \"$p=$(cat \"$p\")\"; done 2>/dev/null"
    )
    used = maxc = None
    for line in (host.run(["sh", "-c", cmd]).get("stdout") or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            n = int(v.strip())
        except ValueError:
            continue
        if k.endswith("_count"):
            used = n
        elif k.endswith("_max"):
            maxc = n
    if used is None and maxc is None:
        return {"status": "unavailable", "note": "nf_conntrack module may not be loaded"}
    res = {"used": used, "max": maxc}
    if used is not None and maxc:
        pct = round(100 * used / maxc, 1)
        res["pct"] = pct
        res["pressure"] = "low" if pct < 60 else "moderate" if pct < 85 else "high"
    return res


# ── pending_updates ───────────────────────────────────────────────────────────────────────────

_FW_KEYS = (
    "firmver", "buildno", "extendno",
    "webs_state_update", "webs_state_flag", "webs_state_info", "webs_state_error",
    "webs_state_level", "sig_state_flag", "sig_state_info", "bwdpi_db_version",
)


def _pending_updates(host, out, check) -> dict:
    helper = "/usr/sbin/webs_update.sh"
    helper_present = host.run(["ls", helper]).get("rc") == 0
    checked_now = False
    if check and helper_present:
        host.run([helper], timeout=120)   # phones home to ASUS; mutates webs_state_*
        checked_now = True

    nv = _nvram_many(host, _FW_KEYS)
    composed = ".".join(p for p in (nv.get("firmver"), nv.get("buildno"), nv.get("extendno")) if p)

    flag, upd, err = nv.get("webs_state_flag", ""), nv.get("webs_state_update", ""), nv.get("webs_state_error", "")
    if flag == "1":
        available = True
    elif flag == "0" and upd == "1":
        available = False
    else:
        available = None   # never checked / inconclusive — NOT "up to date"

    firmware = {
        "available": available,
        "available_version": nv.get("webs_state_info") or None,
        "last_check_ok": (err in ("", "0")) if (upd or err) else None,
        "error": err or None,
        "level": nv.get("webs_state_level") or None,
        "checked_now": checked_now,
        "confidence": "direct_config" if (flag or upd) else "unknown",
        "source": "nvram webs_state_*",
        "note": "ASUS-reported stock-firmware availability; Merlin updates are out-of-band (manual). "
                "'available: false' = no newer ASUS stock fw, NOT 'definitively current' "
                "(webs_state_* can be sticky/stale across reboots/upgrades).",
    }
    if checked_now:
        firmware["note"] += " State keys may lag the check; re-call after ~30s for a settled result."

    sig = nv.get("sig_state_info") or nv.get("bwdpi_db_version") or ""
    aiprotection = {
        "signature_version": sig or None,
        "flag": nv.get("sig_state_flag") or None,
        "confidence": "direct_config" if sig else "unknown",
        "source": "nvram sig_state_*/bwdpi_db_version",
        "note": "empty is 'unknown' (AiProtection may be off/unlicensed), not 'no signatures'.",
    }

    res = {
        "current": {"firmver": nv.get("firmver") or None, "buildno": nv.get("buildno") or None,
                    "extendno": nv.get("extendno") or None, "composed": composed or "unknown"},
        "firmware": firmware,
        "aiprotection": aiprotection,
    }
    if check and not helper_present:
        res["check_note"] = f"{helper} not found; returned last-known nvram state only."
    return res


# ── internet_exposure ─────────────────────────────────────────────────────────────────────────

def _parse_vts_rulelist(raw: str) -> list:
    """Merlin port-forward / virtual-server rules: records split on '<', fields on '>', order
    name>extport>intip>intport>proto[>srcip] (5 OR 6 fields — handle both, no index error).
    Internal IPs are LAN-private (safe to return); srcip restriction materially lowers exposure."""
    rules = []
    for rec in raw.split("<"):
        rec = rec.strip()
        if not rec:
            continue
        f = rec.split(">")
        rules.append({
            "name": f[0] if len(f) > 0 else None,
            "ext_port": f[1] if len(f) > 1 else None,
            "int_ip": f[2] if len(f) > 2 else None,
            "int_port": f[3] if len(f) > 3 else None,
            "proto": f[4] if len(f) > 4 else None,
            "src_restrict": f[5] if len(f) > 5 and f[5] else None,
        })
    return rules


def _parse_upnp_leases(raw: str) -> list:
    """miniupnpd /tmp/upnp.leases lines: PROTO:extPort:intIP:intPort:...:desc."""
    leases = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        p = line.split(":")
        if len(p) >= 4:
            leases.append({"proto": p[0], "ext_port": p[1], "int_ip": p[2], "int_port": p[3]})
        else:
            leases.append({"raw": line[:80]})
    return leases


def _parse_listeners(raw: str) -> list:
    """Listening sockets bound to a wildcard address from netstat/ss -tunlp output (Proto in
    col 0, Local Address in col 3 for both tcp and udp on BusyBox; program is the trailing
    PID/Program token when -p attribution is available)."""
    listeners = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].lower().startswith(("tcp", "udp")):
            continue
        local = parts[3]
        if ":" not in local:
            continue
        addr, _, port = local.rpartition(":")
        if addr not in ("0.0.0.0", "*", "::", "[::]"):
            continue
        prog = parts[-1] if "/" in parts[-1] else None
        listeners.append({"proto": parts[0], "addr": addr, "port": port, "program": prog})
    return listeners


def _internet_exposure(host, out) -> dict:
    nv = _nvram_many(host, _EXPOSURE_NVRAM_KEYS)

    ra_en, ra_conf = _flag(nv.get("misc_http_x"))
    remote_admin = {
        "enabled": ra_en, "confidence": ra_conf,
        "http_port": nv.get("misc_httpport_x") or None,
        "https_port": nv.get("misc_httpsport_x") or None,
        "ui_proto": {"0": "http", "1": "https", "2": "both"}.get(
            nv.get("http_enable"), nv.get("http_enable") or None),
        "source": "nvram misc_http_x",
    }

    # SSH — 1 vs 2 (LAN vs LAN+WAN) has flipped across firmwares; never guess LAN from a nonzero value
    ssh_raw = (nv.get("sshd_enable") or "").strip()
    if ssh_raw == "":
        ssh = {"enabled": None, "scope": "unknown", "confidence": "unknown"}
    elif ssh_raw == "0":
        ssh = {"enabled": False, "scope": "off", "confidence": "direct_config"}
    else:
        ssh = {"enabled": True, "scope": "unknown (verify)",
               "confidence": "ambiguous_vendor_semantics",
               "raw_sshd_enable": ssh_raw, "sshd_wan": nv.get("sshd_wan") or None}
    ssh["port"] = nv.get("sshd_port") or "22"
    ssh["source"] = "nvram sshd_enable/sshd_wan"

    tl_en, tl_conf = _flag(nv.get("telnetd_enable"))
    telnet = {"enabled": tl_en, "confidence": tl_conf, "source": "nvram telnetd_enable",
              "note": "telnet is plaintext; any enablement is a finding"}

    pf_en, pf_conf = _flag(nv.get("vts_enable_x"))
    rules = _parse_vts_rulelist(nv.get("vts_rulelist") or "")
    port_forwards = {"enabled": pf_en, "confidence": pf_conf, "count": len(rules), "rules": rules,
                     "source": "nvram vts_enable_x/vts_rulelist"}

    up_en, up_conf = _flag(nv.get("upnp_enable"))
    leases_r = host.run(["cat", "/tmp/upnp.leases"])
    leases_body = (leases_r.get("stdout") or "").strip()
    if leases_r.get("rc") == 0 and leases_body:
        upnp = {"config_enabled": up_en, "active_mappings": _parse_upnp_leases(leases_body),
                "confidence": "runtime_observed", "source": "/tmp/upnp.leases"}
    elif up_en:   # file absent != feature off (miniupnpd may have just flushed/restarted)
        upnp = {"config_enabled": True, "active_mappings": [],
                "detail": "no active mappings confirmed (leases file absent/empty)",
                "confidence": "direct_config", "source": "nvram upnp_enable"}
    else:
        upnp = {"config_enabled": up_en, "active_mappings": [], "confidence": up_conf,
                "source": "nvram upnp_enable"}

    dmz_ip = (nv.get("dmz_ip") or "").strip()
    dmz = ({"enabled": True, "host": dmz_ip, "confidence": "direct_config"} if dmz_ip
           else {"enabled": None, "confidence": "unknown", "note": "empty dmz_ip is unknown, not off"})
    dmz["source"] = "nvram dmz_ip"

    dd_en, dd_conf = _flag(nv.get("ddns_enable_x"))
    ddns = {"enabled": dd_en, "confidence": dd_conf,
            "provider": nv.get("ddns_server_x") or None,
            "hostname": nv.get("ddns_hostname_x") or None,   # public DNS name; passwd never read
            "source": "nvram ddns_*"}

    wd_en, wd_conf = _flag(nv.get("enable_webdav"))
    daemon = host.run(["sh", "-c", "ps 2>/dev/null | grep -iE 'webdav|aicloud' | grep -v grep"])
    aicloud_webdav = {
        "enabled": wd_en, "confidence": wd_conf,
        "smart_access": nv.get("webdav_smartaccess") or None,
        "aidisk": nv.get("webdav_aidisk") or None,
        "daemon_running": bool((daemon.get("stdout") or "").strip()),
        "source": "nvram enable_webdav/webdav_* + ps",
        "note": "AiCloud smart-access is a WAN tunnel via ASUS (analogous to QuickConnect).",
    }

    ov_en, ov_conf = _flag(nv.get("vpn_server_enable"))
    wg_en, wg_conf = _flag(nv.get("wgs_enable"))
    vpn_servers = {
        "openvpn": {"enabled": ov_en, "confidence": ov_conf,
                    "state1": nv.get("vpn_server1_state") or None,
                    "state2": nv.get("vpn_server2_state") or None,
                    "port": nv.get("vpn_server_port") or None,
                    "proto": nv.get("vpn_server_proto") or None},
        "wireguard": {"enabled": wg_en, "confidence": wg_conf, "port": nv.get("wgs_port") or None},
        "source": "nvram vpn_server*/wgs_* (keys/certs never read)",
    }

    v6_raw = (nv.get("ipv6_fw_enable") or "").strip()
    ipv6 = ({"firewall_enabled": v6_raw == "1", "confidence": "direct_config"} if v6_raw
            else {"firewall_enabled": None, "confidence": "unknown"})
    ipv6.update({"service": nv.get("ipv6_service") or None,
                 "open_rules": nv.get("ipv6_fw_rulelist") or None,
                 "source": "nvram ipv6_fw_enable/ipv6_fw_rulelist",
                 "note": "ipv6_fw_enable=0 means inbound IPv6 firewall OFF (NAT does not protect v6)."})

    ftp_en, ftp_conf = _flag(nv.get("enable_ftp"))
    fw_en, _ = _flag(nv.get("ftp_wanac"))
    ftp_samba = {"ftp_enabled": ftp_en, "ftp_wan": fw_en, "confidence": ftp_conf,
                 "mode": nv.get("st_ftp_mode") or None, "source": "nvram enable_ftp/ftp_wanac"}

    pt_en, pt_conf = _flag(nv.get("autofw_enable"))
    pt_count = len([r for r in (nv.get("autofw_rulelist") or "").split("<") if r.strip()])
    port_trigger = {"enabled": pt_en, "confidence": pt_conf, "count": pt_count,
                    "source": "nvram autofw_enable/autofw_rulelist"}

    # wan_listening — ground truth (UDP matters: WireGuard/OpenVPN-UDP). Visibility metadata so a
    # sparse list isn't over-trusted (BusyBox netstat may lack -p / under-report udp / omit IPv6).
    ns = host.run(["netstat", "-tunlp"])
    ns_body = ns.get("stdout") or ""
    if ns.get("rc") == 0 and ns_body.strip():
        listeners = _parse_listeners(ns_body)
        low = ns_body.lower()
        visibility = {"tcp": "ok" if "tcp" in low else "none",
                      "udp": "ok" if "udp" in low else "none",
                      "process_names": any(l.get("program") for l in listeners)}
        source = "netstat -tunlp"
    else:
        ss = host.run(["ss", "-tunlp"])
        ss_body = ss.get("stdout") or ""
        if ss.get("rc") == 0 and ss_body.strip():
            listeners = _parse_listeners(ss_body)
            visibility = {"tcp": "partial", "udp": "partial",
                          "process_names": any(l.get("program") for l in listeners)}
            source = "ss -tunlp (netstat unavailable)"
        else:
            listeners = []
            visibility = {"tcp": "none", "udp": "none", "process_names": False,
                          "note": "neither netstat nor ss returned usable output"}
            source = "netstat/ss unavailable"
    wan_listening = {"listeners": listeners, "listener_visibility": visibility,
                     "confidence": "heuristic", "source": source,
                     "note": "0.0.0.0/:: listeners are firewall-gated; not necessarily WAN-reachable"}

    return {
        "remote_admin": remote_admin, "ssh": ssh, "telnet": telnet,
        "port_forwards": port_forwards, "upnp": upnp, "dmz": dmz, "ddns": ddns,
        "aicloud_webdav": aicloud_webdav, "vpn_servers": vpn_servers, "ipv6": ipv6,
        "ftp_samba": ftp_samba, "port_trigger": port_trigger, "wan_listening": wan_listening,
        "note": "Configured intent != live WAN ingress; listeners are firewall-gated; secrets are "
                "never returned (nvram read via a fixed safe-key allowlist).",
    }


if __name__ == "__main__":
    main()
