# router-hands (planned)

Second appliance relay, reusing `mage_hands_core`. Placeholder — not yet implemented.

The point of the monorepo split is that this directory should be small: the auth, audit,
gated `run()`, and read-policy machinery all come from `common/`. router-hands only needs to
supply **how to execute on the router** (a Runner) and **what to expose** (tools).

## Sketch

```python
from mage_hands_core import (Config, build_server, run_server,
                             register_run_tool, PathPolicy, fs_reader, register_read_file)

cfg  = Config.from_env()
mcp  = build_server("router-hands", INSTRUCTIONS, cfg)
host = <Runner>          # pick based on the router:
                         #   - ShellRunner  if the relay runs ON the router (e.g. OpenWrt container/native)
                         #   - NsenterRunner if it's a privileged container on a Linux router host
                         #   - a new SSHRunner (add to common/exec.py) if the relay runs beside the
                         #     router and reaches it over SSH

# @mcp.tool() ... router tools: interface_status, dhcp_leases, firewall_show,
#                 reload_config, restart_service, ...

register_read_file(mcp, PathPolicy(allow=[...], deny=[...]), fs_reader("/host"))
register_run_tool(mcp, host)
run_server(mcp, cfg)
```

## What likely needs to move into `common/` when this is built

- An `SSHRunner` (if the relay talks to the router over SSH rather than running on it).
- Any router platforms that aren't Linux-shell friendly may need a different read strategy
  than `fs_reader` (e.g. a reader that fetches via the Runner). `register_read_file` already
  takes an arbitrary `reader` callable for exactly this.

Each router gets its own `RELAY_TOKEN`, MagicDNS name, and `claude mcp add` entry (e.g.
`router1`), same as the NAS fleet.
