"""mage-hands :: synology-hands — MCP relay for administering a Synology NAS host.

Runs in a privileged container (pid:host) and drives the host via ``nsenter -t 1``, so it can
use the host's own toolchain (docker, smartctl, synoservicectl, synowebapi, ...). All the
security machinery — token auth, forensic audit, the gated run() tool, the read path policy —
comes from mage_hands_core; this module just registers Synology-specific tools.
"""

from __future__ import annotations

from mage_hands_core import (
    Config,
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

# Read policy — widen ALLOW / tighten DENY per box before fleet rollout.
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
    host = NsenterRunner()
    mcp = build_server("synology-hands", INSTRUCTIONS, cfg)

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

    # ---- Tier B: controlled mutation (narrow, audited) ----
    @mcp.tool(annotations={"destructiveHint": True})
    def restart_container(name: str) -> dict:
        """Restart a single container by name."""
        return host.run(["docker", "restart", name])

    @mcp.tool(annotations={"destructiveHint": True})
    def restart_service(name: str) -> dict:
        """Restart a DSM service (synoservicectl --restart <name>)."""
        return host.run(["synoservicectl", "--restart", name])

    # ---- Tier A: policied file read ----
    register_read_file(mcp, PathPolicy(READ_ALLOW, READ_DENY), fs_reader("/host"))

    # ---- Tier C: gated raw exec ----
    register_run_tool(mcp, host)

    run_server(mcp, cfg)


if __name__ == "__main__":
    main()
