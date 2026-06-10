"""mage_hands_core — reusable building blocks for mage-hands MCP relays.

An appliance relay (synology-hands, router-hands, ...) is assembled from:

    cfg  = Config.from_env()
    mcp  = build_server("synology-hands", INSTRUCTIONS, cfg)   # auth + audit + lifespan
    host = NsenterRunner()                                     # how to execute on the target
    # ... register appliance-specific @mcp.tool() functions ...
    policy = PathPolicy(allow, deny)
    register_read_file(mcp, policy, fs_reader("/host", policy=policy))
    register_run_tool(mcp, host)                               # gated Tier-C raw exec
    run_server(mcp, cfg)

The security-critical pieces (token auth, forensic audit, the dry-run/replay-token gate
on raw exec, and the read path policy) live here so every appliance inherits them.
"""

from .config import Config
from .server import build_server, run_server
from .exec import ShellRunner, NsenterRunner, SSHRunner, register_run_tool, DEFAULT_DENY
from .policy import PathPolicy, fs_reader, runner_reader, register_read_file
from .audit import AuditMiddleware, setup_audit, touch_activity, truncate

__all__ = [
    "Config",
    "build_server",
    "run_server",
    "ShellRunner",
    "NsenterRunner",
    "SSHRunner",
    "register_run_tool",
    "DEFAULT_DENY",
    "PathPolicy",
    "fs_reader",
    "runner_reader",
    "register_read_file",
    "AuditMiddleware",
    "setup_audit",
    "touch_activity",
    "truncate",
]
