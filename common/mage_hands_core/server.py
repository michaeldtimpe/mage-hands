"""Assemble a FastMCP relay with shared auth, audit, and lifecycle wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastmcp import FastMCP

from .audit import AuditMiddleware, setup_audit
from .auth import build_token_verifier
from .config import Config


def build_server(name: str, instructions: str, config: Config) -> FastMCP:
    """Create a FastMCP server with token auth, a lifespan flush, and audit middleware.

    Register appliance-specific tools on the returned server, then call ``run_server``.
    """
    audit_log = setup_audit(config.audit_dir)

    @asynccontextmanager
    async def lifespan(app):
        # uvicorn drains in-flight calls on SIGTERM (see run_server); flush audit on shutdown.
        yield
        for handler in audit_log.handlers:
            handler.flush()

    mcp = FastMCP(
        name,
        instructions=instructions,
        auth=build_token_verifier(config.token),
        lifespan=lifespan,
    )
    mcp.add_middleware(
        AuditMiddleware(config.node_id, config.audit_dir, config.allowed_users)
    )
    return mcp


def run_server(mcp: FastMCP, config: Config) -> None:
    mcp.run(
        transport="http",
        host=config.host,
        port=config.port,
        path=config.path,
        uvicorn_config={"timeout_graceful_shutdown": config.graceful_timeout},
    )
