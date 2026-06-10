"""Runtime configuration, loaded from environment (see .env.example)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _split_list(val: str | None) -> list[str]:
    """Split a comma/newline env value into stripped, non-empty items.

    Set-but-empty (``""``) and unset both yield ``[]`` — i.e. a no-op, never a wipe. This is
    load-bearing for the additive policy knobs: an empty override must not silently erase a
    default deny path.
    """
    if not val:
        return []
    return [p.strip() for p in re.split(r"[\n,]", val) if p.strip()]


@dataclass
class Config:
    token: str
    node_id: str
    allowed_users: set[str] = field(default_factory=set)
    audit_dir: str = "/var/log/mcp"
    host: str = "0.0.0.0"        # inside the container; published only to host loopback
    port: int = 8787
    path: str = "/mcp"
    graceful_timeout: int = 30   # seconds uvicorn drains in-flight calls on shutdown

    # --- output cap: truncates run() AND every Tier-A tool's stdout/stderr ---
    # ``output_cap`` is the working cap (already clamped to <= output_cap_max). ``output_cap_max``
    # is a hard ceiling no env value or per-call ``max_bytes`` may exceed — it keeps a giant read
    # from becoming an MCP self-DoS or blowing past the client context window.
    output_cap: int = 65_536
    output_cap_max: int = 2_097_152

    # --- policy tuning (additive-first) ---
    # ``run_deny_extra`` is APPENDED to DEFAULT_DENY (never replaces it). The read lists are
    # additive too, unless ``read_policy_override`` is set, in which case the appliance defaults
    # are fully replaced by the *_EXTRA values (logged loudly at startup).
    run_deny_extra: list[str] = field(default_factory=list)
    read_allow_extra: list[str] = field(default_factory=list)
    read_deny_extra: list[str] = field(default_factory=list)
    read_policy_override: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("RELAY_TOKEN")
        if not token:
            raise SystemExit("RELAY_TOKEN is required (set it in .env)")
        # Sanity floor, not a cryptographic bound: this token is the only credential between
        # the tailnet and root on the target. Docs prescribe `openssl rand -hex 32` (64 chars).
        if len(token) < 16:
            raise SystemExit(
                f"RELAY_TOKEN is too short ({len(token)} chars; minimum 16). "
                "Generate one with: openssl rand -hex 32"
            )

        output_cap_max = int(os.environ.get("OUTPUT_CAP_MAX", str(2 * 1024 * 1024)))
        output_cap = min(int(os.environ.get("OUTPUT_CAP", "65536")), output_cap_max)

        # Fail loud at config-load if an extra deny pattern is a bad regex — never at call time,
        # where it would surface as a confusing per-command error long after deploy.
        run_deny_extra = _split_list(os.environ.get("RUN_DENY_EXTRA"))
        for pat in run_deny_extra:
            try:
                re.compile(pat)
            except re.error as exc:
                raise SystemExit(f"RUN_DENY_EXTRA: invalid regex {pat!r}: {exc}")

        return cls(
            token=token,
            node_id=os.environ.get("NODE_ID") or os.uname().nodename,
            allowed_users={
                u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()
            },
            audit_dir=os.environ.get("AUDIT_DIR", "/var/log/mcp"),
            host=os.environ.get("BIND_HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8787")),
            path=os.environ.get("MCP_PATH", "/mcp"),
            graceful_timeout=int(os.environ.get("GRACEFUL_TIMEOUT", "30")),
            output_cap=output_cap,
            output_cap_max=output_cap_max,
            run_deny_extra=run_deny_extra,
            read_allow_extra=_split_list(os.environ.get("READ_ALLOW_EXTRA")),
            read_deny_extra=_split_list(os.environ.get("READ_DENY_EXTRA")),
            read_policy_override=os.environ.get("READ_POLICY_OVERRIDE", "").lower()
            in ("1", "true", "yes"),
        )
