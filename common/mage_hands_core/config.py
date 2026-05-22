"""Runtime configuration, loaded from environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("RELAY_TOKEN")
        if not token:
            raise SystemExit("RELAY_TOKEN is required (set it in .env)")
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
        )
