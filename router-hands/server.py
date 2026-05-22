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
import sys
import time

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
    "diagnostics, clients, dhcp_leases, wan_status, interfaces, firewall_show). For run() (only "
    "present when enabled): always dry-run first (call without exec_token), show the user the "
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
# to a router: jffs/USB wipes, mtd/flash writes, factory reset, and boot-state tampering. reboot
# stays blocked by inheriting DEFAULT_DENY — on a router it drops all connectivity, so leaving the
# core availability backstop in place is the safe default.
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
]

# Tier-B service allowlist: name → fixed verb `service restart_<name>`. Keeps the tool from being
# used for arbitrary `service` verbs (start_*/stop_*/reboot/firmware helpers).
ALLOWED_SERVICES = {
    "dnsmasq", "wireless", "firewall", "wan", "httpd", "net", "samba", "nasapps",
    "vpnclient1", "vpnclient2", "vpnclient3", "vpnclient4", "vpnclient5",
    "vpnserver1", "vpnserver2", "wgc", "wgs",
}


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

    # ---- Tier B: controlled mutation (narrow, audited) ----
    @mcp.tool(annotations={"destructiveHint": True})
    def restart_service(name: str) -> dict:
        """Restart a router service via Merlin `service restart_<name>` (verb is fixed to
        restart_<name>, NOT `<name> restart`). Restricted to a vetted allowlist so the tool can't
        invoke arbitrary `service` verbs. Restarting wan/wireless/firewall briefly drops
        connectivity (the SSH transport may blip → an indeterminate result; re-inspect)."""
        if name not in ALLOWED_SERVICES:
            return {"refused": True, "reason": f"service {name!r} not allowed",
                    "allowed": sorted(ALLOWED_SERVICES)}
        return host.run(["service", f"restart_{name}"])

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

    # ---- Tier C: gated raw exec — OPT-IN on a router (default OFF) ----
    # Arbitrary root on a soft-brickable target is a different blast radius than NAS inspection,
    # so run() is only registered when ROUTER_ENABLE_RUN is explicitly truthy. Denylist is the
    # core DEFAULT_DENY + router extras + the operator's additive RUN_DENY_EXTRA.
    if os.environ.get("ROUTER_ENABLE_RUN", "").lower() in ("1", "true", "yes"):
        register_run_tool(
            mcp,
            host,
            deny_patterns=DEFAULT_DENY + ROUTER_DENY_EXTRA + cfg.run_deny_extra,
            output_cap=cfg.output_cap,
        )
    else:
        print(
            "[mage-hands] run() is disabled — set ROUTER_ENABLE_RUN=true to enable gated raw exec.",
            file=sys.stderr,
            flush=True,
        )

    run_server(mcp, cfg)


if __name__ == "__main__":
    main()
