"""Forensic audit logging + caller-identity enforcement.

Every tool call is logged as one JSON line with: timestamp, correlation id, node id, the
Tailscale-verified caller identity (injected by ``tailscale serve``), tool name, arguments,
status, and duration. The log uses a rotating handler so it can't grow unbounded, and each
call also atomically updates ``last_activity`` for the idle-shutdown watchdog.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from logging.handlers import RotatingFileHandler

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers


def setup_audit(audit_dir: str) -> logging.Logger:
    os.makedirs(audit_dir, exist_ok=True)
    log = logging.getLogger("mage_hands.audit")
    if not log.handlers:
        log.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            os.path.join(audit_dir, "audit.jsonl"),
            maxBytes=10_000_000,
            backupCount=10,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.propagate = False
    return log


def touch_activity(audit_dir: str) -> None:
    """Atomically record 'now' so the idle watchdog can detect inactivity."""
    path = os.path.join(audit_dir, "last_activity")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(time.time()))
    os.replace(tmp, path)


def truncate(text: str | None, limit: int = 4000) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"...<+{len(text) - limit} bytes truncated>"


class AuditMiddleware(Middleware):
    """Logs every tool call and (optionally) enforces a caller-identity allowlist.

    NOTE: ``get_http_headers`` strips ``authorization``/``host`` by default; we only need the
    pass-through ``tailscale-user-*`` headers here, which we request explicitly. Token auth
    itself is handled earlier by StaticTokenVerifier (see auth.py).
    """

    def __init__(self, node_id: str, audit_dir: str, allowed_users: set[str] | None = None):
        self.node_id = node_id
        self.audit_dir = audit_dir
        self.allowed_users = allowed_users or set()
        self.log = setup_audit(audit_dir)

    async def on_call_tool(self, ctx: MiddlewareContext, call_next):
        headers = get_http_headers(include={"tailscale-user-login", "tailscale-user-name"})
        user = headers.get("tailscale-user-login", "?")

        # Defense-in-depth: even with a valid token, reject unexpected tailnet identities.
        if self.allowed_users and user not in self.allowed_users:
            raise PermissionError(f"identity {user!r} is not in ALLOWED_USERS")

        cid = secrets.token_hex(8)
        started = time.time()
        status = "ok"
        try:
            return await call_next(ctx)
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            touch_activity(self.audit_dir)
            self.log.info(
                json.dumps(
                    {
                        "ts": time.time(),
                        "cid": cid,
                        "node": self.node_id,
                        "user": user,
                        "tool": getattr(ctx.message, "name", "?"),
                        "args": getattr(ctx.message, "arguments", None),
                        "status": status,
                        "ms": round((time.time() - started) * 1000),
                    },
                    default=str,
                )
            )
