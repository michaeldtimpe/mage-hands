"""Bearer-token authentication for the relay.

We use fastmcp's built-in StaticTokenVerifier (passed as ``auth=`` to FastMCP) so that an
unauthenticated request gets a spec-compliant HTTP 401 at the transport layer, BEFORE any
tool is dispatched. The import path has moved across fastmcp builds, so we probe the known
locations and fail loudly with a clear message (caught by the Phase-1 smoke check).
"""

from __future__ import annotations

_CANDIDATE_MODULES = (
    "fastmcp.server.auth.providers.jwt",
    "fastmcp.server.auth.providers.bearer",
    "fastmcp.server.auth.providers.static",
    "fastmcp.server.auth",
)


def build_token_verifier(token: str):
    """Return a StaticTokenVerifier accepting a single shared bearer ``token``."""
    last_error: Exception | None = None
    for module_name in _CANDIDATE_MODULES:
        try:
            module = __import__(module_name, fromlist=["StaticTokenVerifier"])
            StaticTokenVerifier = getattr(module, "StaticTokenVerifier")
        except (ImportError, AttributeError) as exc:
            last_error = exc
            continue
        return StaticTokenVerifier(
            tokens={token: {"client_id": "mage-hands", "scopes": []}}
        )
    raise ImportError(
        "Could not locate StaticTokenVerifier in the installed fastmcp. "
        f"Tried: {', '.join(_CANDIDATE_MODULES)}. Last error: {last_error}"
    )
