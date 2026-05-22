"""Command execution strategies and the gated Tier-C ``run()`` tool.

A *Runner* abstracts "how do I execute a command on the target":
  - ``ShellRunner``  runs in the current namespace (local relay).
  - ``NsenterRunner`` enters the host namespaces from a privileged container (NAS pattern).
A future router relay can add e.g. an ``SSHRunner`` without touching the gating logic.

``register_run_tool`` adds a single arbitrary-root ``run()`` tool whose danger is mitigated
by two layers that every appliance inherits:
  1. a hard *denylist* of catastrophic patterns, refused regardless of confirmation;
  2. a dry-run / one-time *exec_token* gate — the first call returns a token bound to the
     exact command (short TTL); execution requires replaying that exact token. This stops a
     single hallucinated or over-eager follow-up call from mutating the host.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import subprocess
import time
from typing import Annotated, Protocol

from pydantic import Field

from .audit import truncate


class Runner(Protocol):
    def run(self, argv: list[str], timeout: int = 60) -> dict: ...


def _exec(argv: list[str], timeout: int) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {
        "rc": proc.returncode,
        "stdout": truncate(proc.stdout),
        "stderr": truncate(proc.stderr),
    }


class ShellRunner:
    """Run commands directly in the relay's own namespace."""

    def run(self, argv: list[str], timeout: int = 60) -> dict:
        return _exec(argv, timeout)


class NsenterRunner:
    """Run commands in the HOST namespaces from a privileged container (requires pid:host).

    Container environment variables do NOT propagate into host execution (a security
    benefit: RELAY_TOKEN can't leak into host process listings). Pass any needed env inline.
    """

    PREFIX = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]

    def run(self, argv: list[str], timeout: int = 60) -> dict:
        return _exec(self.PREFIX + argv, timeout)


# Catastrophic patterns refused even with a valid exec_token. This is a backstop, NOT a
# complete safety guarantee — keep the relay ephemeral and audited.
DEFAULT_DENY = [
    # Wiping the root or a whole storage pool — incl. trailing-slash and glob forms
    # (rm -rf /, /*, /volume1, /volume1/, /volume1/*). Targeted deletes UNDER a volume
    # (e.g. /volume1/docker/app/cache) are intentionally allowed — that's legitimate work.
    r"rm\s+-[a-z]*r[a-z]*f?\s+/\*?(?:\s|$)",
    r"rm\s+-[a-z]*r[a-z]*f?\s+/volume\d+/?\*?(?:\s|$)",
    r"\bmkfs\b",
    r"\bdd\b.*\bof=/dev/",
    r"\bmdadm\b.*--(?:remove|fail|zero-superblock)",
    r"chmod\s+-R[^/]*\s/\*?(?:\s|$)",          # recursive chmod on / (incl. /*)
    r"chown\s+-R[^/]*\s/\*?(?:\s|$)",
    r"\b(?:fdisk|parted|sgdisk)\b",
    r">\s*/dev/sd",
    r"\bsynostorage\b.*--(?:delete|remove)",
]


def register_run_tool(
    mcp,
    runner: Runner,
    *,
    ttl: int = 300,
    timeout: int = 300,
    deny_patterns: list[str] | None = None,
):
    deny = [re.compile(p) for p in (deny_patterns or DEFAULT_DENY)]
    pending: dict[str, tuple[str, float]] = {}

    @mcp.tool(annotations={"destructiveHint": True})
    def run(
        command: Annotated[str, Field(description="shell command, runs as ROOT on the target host")],
        exec_token: Annotated[
            str | None,
            Field(description="replay token from a prior dry-run; required to execute"),
        ] = None,
    ) -> dict:
        """Tier C — arbitrary root command on the host.

        Call once WITHOUT exec_token to get a dry-run preview plus a one-time token, then call
        again replaying that exact token (same command) to execute. Catastrophic patterns are
        refused outright.
        """
        if any(p.search(command) for p in deny):
            return {"refused": True, "reason": "matches catastrophic-pattern denylist"}

        digest = hashlib.sha256(command.encode()).hexdigest()

        if exec_token is None:
            token = secrets.token_urlsafe(12)
            pending[token] = (digest, time.time() + ttl)
            return {
                "dry_run": True,
                "would_run": command,
                "exec_token": token,
                "ttl_seconds": ttl,
                "note": "re-call run() with this exec_token (same exact command) to execute",
            }

        record = pending.pop(exec_token, None)
        if (
            record is None
            or not hmac.compare_digest(record[0], digest)
            or record[1] < time.time()
        ):
            return {
                "refused": True,
                "reason": "invalid/expired exec_token or command changed; request a fresh dry-run",
            }

        return runner.run(["sh", "-c", command], timeout=timeout)

    return run
