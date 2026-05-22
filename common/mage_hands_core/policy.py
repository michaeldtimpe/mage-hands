"""Path policy + the policied Tier-A ``read_file`` tool.

``read_file`` looks harmless but is the most likely accidental exfiltration vector (an agent
deciding to "inspect this config" reads /etc/shadow, ssh keys, Tailscale state, ...). So
reads are constrained two ways:
  - ``PathPolicy`` enforces an allowlist of roots and a denylist of secret paths, after
    lexically normalizing the requested absolute path (resolves ``..`` without touching the FS);
  - ``fs_reader`` performs the actual read by joining a mount prefix (``/host`` on the NAS),
    resolving symlinks, and re-checking containment as a final guard.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Callable

from pydantic import Field

from .audit import truncate


class PathPolicy:
    def __init__(self, allow: list[str], deny: list[str] | None = None):
        self.allow = [a.rstrip("/") for a in allow]
        self.deny = [d.rstrip("/") for d in (deny or [])]

    def check(self, host_abs: str) -> str:
        """Validate a host-absolute path against allow/deny. Returns the normalized path."""
        if not host_abs.startswith("/"):
            raise ValueError("absolute path required")
        norm = os.path.normpath(host_abs)
        for d in self.deny:
            if norm == d or norm.startswith(d + "/"):
                raise PermissionError("denied by read policy")
        if not any(norm == a or norm.startswith(a + "/") for a in self.allow):
            raise PermissionError("path not in allowed read roots")
        return norm


def fs_reader(prefix: str = "/host", max_bytes: int = 200_000) -> Callable[[str], str]:
    """Build a reader that maps a host-absolute path under ``prefix`` and reads it safely."""
    base = Path(prefix).resolve()

    def read(host_abs: str) -> str:
        target = (base / host_abs.lstrip("/")).resolve()  # join THEN resolve symlinks
        if target != base and base not in target.parents:
            raise ValueError("path traversal blocked")
        return truncate(target.read_text(errors="replace"), max_bytes)

    return read


def runner_reader(runner, max_bytes: int = 200_000) -> Callable[[str], str]:
    """Build a ``read_file`` reader that fetches a file via ``runner.run(["cat", path])``.

    For relays that reach the target through a Runner instead of a mounted filesystem (e.g. the
    SSH router relay, where there is no ``/host`` mount). Generic over any Runner.

    SECURITY — this is WEAKER than ``fs_reader`` and the difference is load-bearing: ``fs_reader``
    resolves symlinks locally and re-checks containment, but here the read happens on the *remote*
    host, so ``PathPolicy.check`` (purely lexical) is the ONLY guard and it CANNOT see remote
    symlinks. Treat this as best-effort constrained reading on a *trusted* appliance, not
    filesystem confinement. Risk calibration for a Merlin router: ``/proc/net`` is safe (no
    meaningful symlinks); ``/var`` and ``/tmp`` are world-writable and the highest symlink risk;
    ``/jffs`` is trusted but user-writable. The explicit READ_DENY list is the real boundary, so
    keep ALLOW roots conservative and DENY every secret/world-writable trap.
    """

    def read(path: str) -> str:
        # Pass cap=max_bytes so a file read can use the larger read cap rather than the Runner's
        # default command-output cap; cat on a dir/missing file returns rc!=0 → a clean error.
        res = runner.run(["cat", path], cap=max_bytes)
        if res.get("rc", 1) != 0:
            raise FileNotFoundError(res.get("stderr") or f"cat failed for {path}")
        return truncate(res.get("stdout") or "", max_bytes)

    return read


def register_read_file(mcp, policy: PathPolicy, reader: Callable[[str], str]):
    @mcp.tool()
    def read_file(
        path: Annotated[str, Field(description="absolute host path, e.g. /volume1/docker/app/.env")]
    ) -> dict:
        """Tier A — read a text file, restricted to allowed roots (secret paths are denied)."""
        norm = policy.check(path)
        return {"path": norm, "content": reader(norm)}

    return read_file
