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


def _is_audit_handler(handler, path: str) -> bool:
    """True if ``handler`` is our rotating file handler already writing to ``path``."""
    return (
        isinstance(handler, RotatingFileHandler)
        and os.path.abspath(getattr(handler, "baseFilename", "")) == os.path.abspath(path)
    )


def setup_audit(audit_dir: str) -> logging.Logger:
    """Attach the rotating audit handler for ``audit_dir`` (idempotent).

    The guard here is deliberately specific. An earlier version skipped setup whenever the
    logger had *any* handler (``if not log.handlers``), which meant a single foreign handler
    attached to ``mage_hands.audit`` — a debug handler, pytest's log-capture handler — silently
    suppressed the audit file: calls kept being logged, nothing reached disk, and no error was
    raised. For a forensic trail, failing open like that is worse than not logging at all.
    """
    os.makedirs(audit_dir, exist_ok=True)
    path = os.path.join(audit_dir, "audit.jsonl")
    log = logging.getLogger("mage_hands.audit")
    log.setLevel(logging.INFO)

    if not any(_is_audit_handler(h, path) for h in log.handlers):
        # Drop any audit handler aimed at a *different* directory so a reconfigure doesn't
        # keep writing to the old location as well.
        for h in [h for h in log.handlers if isinstance(h, RotatingFileHandler)]:
            log.removeHandler(h)
            h.close()
        handler = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=10)
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
    """Truncate ``text`` to ``limit`` BYTES of UTF-8 (every cap in this codebase is bytes)."""
    if text is None:
        return None
    if len(text) <= limit and text.isascii():  # fast path: bytes == chars
        return text
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    head = data[:limit].decode("utf-8", errors="ignore")  # never splits a multibyte char
    return head + f"...<+{len(data) - limit} bytes truncated>"


# Bound on the serialized tool-arguments rendering in an audit line. A run() command can be
# arbitrarily large; without a cap one call could bloat audit.jsonl entries past what jq/SIEM
# consumers comfortably parse.
ARGS_CAP = 4000


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
        # Touch at call START as well as in finally: a run() can hold the connection for up to
        # 300s, and an end-only touch lets the idle watchdog tear the relay down mid-call when
        # the call starts near the idle deadline.
        touch_activity(self.audit_dir)

        # Bound the logged arguments: keep the structured object in the common case, degrade to
        # a truncated STRING rendering when oversized (the line itself stays valid JSON).
        args = getattr(ctx.message, "arguments", None)
        if args is not None:
            rendered = json.dumps(args, default=str)
            if len(rendered) > ARGS_CAP:
                args = truncate(rendered, ARGS_CAP)

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
                        "args": args,
                        "status": status,
                        "ms": round((time.time() - started) * 1000),
                    },
                    default=str,
                )
            )
