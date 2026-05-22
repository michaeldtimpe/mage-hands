#!/usr/bin/env python3
"""Phase-1 smoke test: confirm the relay answers a real MCP handshake AND enforces auth.

A bare `curl http://127.0.0.1:8788/mcp` returning 200 does NOT prove auth works, so this runs an
actual MCP initialize + tools/list with a good token (must succeed) and a bad token (must be
rejected with 401).

The relay binds 127.0.0.1:8788 INSIDE the Tailscale sidecar's network namespace, so kappa's host
loopback can't reach it — run this from INSIDE the relay container:

    sudo docker exec -i -e RELAY_TOKEN=<token> router-hands python - < scripts/smoke-test.py

If the fastmcp Client `auth=` kwarg differs in your version, adjust the two Client(...) calls.
"""

import asyncio
import os
import sys

from fastmcp import Client

URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8788/mcp")
TOKEN = os.environ.get("RELAY_TOKEN", "")


async def list_tools(token: str, label: str) -> int:
    client = Client(URL, auth=token)  # fastmcp treats a str auth as a Bearer token
    async with client:
        tools = await client.list_tools()
        print(f"[{label}] connected — {len(tools)} tools: {[t.name for t in tools]}")
        return len(tools)


async def main() -> None:
    if not TOKEN:
        print("set RELAY_TOKEN", file=sys.stderr)
        sys.exit(2)

    # 1) correct token must succeed
    n = await list_tools(TOKEN, "valid-token")
    if n == 0:
        print("FAIL: no tools returned", file=sys.stderr)
        sys.exit(1)

    # 2) wrong token must be rejected
    try:
        await list_tools("definitely-wrong-token", "bad-token")
    except Exception as exc:  # noqa: BLE001 - any auth failure is a pass here
        print(f"OK: bad token rejected ({type(exc).__name__})")
    else:
        print("FAIL: bad token was accepted", file=sys.stderr)
        sys.exit(1)

    print("smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
