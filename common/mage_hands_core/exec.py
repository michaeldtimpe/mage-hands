"""Command execution strategies and the gated Tier-C ``run()`` tool.

A *Runner* abstracts "how do I execute a command on the target":
  - ``ShellRunner``  runs in the current namespace (local relay).
  - ``NsenterRunner`` enters the host namespaces from a privileged container (NAS pattern).
  - ``SSHRunner``    runs on a remote host over SSH (router pattern — the relay runs elsewhere
    and the target only needs SSH; e.g. an Asuswrt-Merlin router with no Docker/nsenter).
A new transport is just a new Runner; the gating logic below is untouched.

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
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from typing import Annotated, Protocol

from pydantic import Field

from .audit import truncate

# Default truncation for command output, in bytes. Overridden per-relay from Config.output_cap;
# applies to run() AND every Tier-A tool (they all go through a Runner).
DEFAULT_OUTPUT_CAP = 65_536

# Host PATH for nsenter'd commands. DSM keeps its own tooling (synopkg, synoservicectl,
# synogetkeyvalue, ...) in /usr/syno/{bin,sbin} and the Tailscale package binary under its
# package tree — none of which are on the container's login-less PATH inside the host
# namespaces. Without this, those commands die with exit 127 (the recurring lesson). We set it
# via /usr/bin/env (absolute, list form — never a shell) so there is no injection surface.
HOST_PATH = (
    "/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin:"
    "/usr/local/bin:/usr/local/sbin:/var/packages/Tailscale/target/bin"
)


class Runner(Protocol):
    def run(self, argv: list[str], timeout: int = 60, cap: int | None = None) -> dict: ...


def _exec(argv: list[str], timeout: int, cap: int) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {
        "rc": proc.returncode,
        "stdout": truncate(proc.stdout, cap),
        "stderr": truncate(proc.stderr, cap),
    }


class ShellRunner:
    """Run commands directly in the relay's own namespace."""

    def __init__(self, cap: int = DEFAULT_OUTPUT_CAP):
        self.cap = cap

    def run(self, argv: list[str], timeout: int = 60, cap: int | None = None) -> dict:
        return _exec(argv, timeout, self.cap if cap is None else cap)


class NsenterRunner:
    """Run commands in the HOST namespaces from a privileged container (requires pid:host).

    Container environment variables do NOT propagate into host execution (a security benefit:
    RELAY_TOKEN can't leak into host process listings). We therefore set an explicit PATH via
    ``/usr/bin/env`` so DSM's ``syno*`` tools and the Tailscale binary resolve. argv is passed as
    a list (no shell), so an absolute argv[0] is simply harmless/redundant with the env wrapper.
    """

    PREFIX = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]

    def __init__(self, cap: int = DEFAULT_OUTPUT_CAP):
        self.cap = cap

    def run(self, argv: list[str], timeout: int = 60, cap: int | None = None) -> dict:
        wrapped = self.PREFIX + ["/usr/bin/env", f"PATH={HOST_PATH}", *argv]
        return _exec(wrapped, timeout, self.cap if cap is None else cap)


# Asuswrt-Merlin's dropbear gives non-interactive SSH sessions a near-empty environment and does
# NOT honor AcceptEnv, so bare tool names (`wl`, `nvram`, `iptables`, `service`) die with exit 127
# unless we set PATH ourselves. /jffs/{sbin,bin} carry Entware/addon tools when present.
_MERLIN_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin:/jffs/sbin:/jffs/bin"

# The shell we invoke for `sh -c ...` payloads (run() + the Tier-A helpers that need a pipe/loop/
# command-substitution). It MUST be an absolute path: Broadcom-based ASUS firmware ships a
# memory-diagnostic multicall binary whose applet is literally named `sh` (the "store-halfword"
# command — its usage banner lists dw/dh/db, sw/sh/sb, fw/fh/fb) in an sbin dir that precedes /bin
# on _MERLIN_PATH. So a BARE `sh -c` resolves to that tool, which rejects -c ("sh: invalid option
# -- 'c'") and silently breaks every shell payload (run(), internet_exposure, pending_updates,
# performance). /bin/sh is busybox ash on Merlin. Override per-box via ROUTER_REMOTE_SHELL.
_DEFAULT_REMOTE_SHELL = "/bin/sh"


class SSHRunner:
    """Run commands on a remote host over SSH (key auth, BatchMode) — the "relay runs elsewhere"
    pattern for targets that can't host the relay (e.g. an Asuswrt-Merlin router: BusyBox ash +
    dropbear, no Docker, no nsenter). The relay runs in a container on the NAS and reaches the
    router over SSH.

    Quoting: the Runner contract hands us a list ``argv`` (``["sh","-c", command]`` from run(),
    or ``["cat", path]`` from a tool). We render it with ``shlex.join`` into ONE POSIX-quoted
    string placed after ``ssh ... --`` — exactly one remote shell evaluation, zero local
    evaluation (we never use shell=True). Assumes a POSIX /bin/sh on the remote (BusyBox ash
    qualifies). Don't replace this with token-passing after ``--``: relying on ssh's space-joining
    of trailing args is fragile across ssh/dropbear builds.

    Security: the private key is a FILE (mounted at runtime, never baked into the image);
    ``BatchMode=yes`` fails fast instead of hanging on a prompt; host identity is pinned via
    ``UserKnownHostsFile`` + ``StrictHostKeyChecking=yes`` (no in-container TOFU). RELAY_TOKEN is
    never sent to the router — only the tool's argv is.
    """

    def __init__(
        self,
        host: str,
        user: str = "admin",
        port: int = 22,
        key_file: str = "/secrets/router_key",
        connect_timeout: int = 10,
        strict_host_key_checking: str = "yes",   # "yes" = pinned; "accept-new" = first-deploy only
        known_hosts: str | None = "/secrets/known_hosts",
        control_persist: int = 60,               # 0 disables ControlMaster multiplexing
        remote_shell: str = _DEFAULT_REMOTE_SHELL,  # absolute shell for `sh -c` payloads (see above)
        cap: int = DEFAULT_OUTPUT_CAP,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_file = key_file
        self.connect_timeout = connect_timeout
        self.strict_host_key_checking = strict_host_key_checking
        self.known_hosts = known_hosts or None
        self.control_persist = control_persist
        self.remote_shell = remote_shell
        self.cap = cap

    def _ssh_argv(self, remote_cmd: str) -> list[str]:
        argv = [
            "ssh",
            "-i", self.key_file,
            "-p", str(self.port),
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", f"StrictHostKeyChecking={self.strict_host_key_checking}",
        ]
        if self.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.control_persist:
            # Reuse one session across the several host.run calls a single tool issues — dropbear
            # handshakes are slow on router CPUs. The control socket lives in the container's
            # per-run tmpfs /tmp, so a container restart never leaves a stale socket behind. Set
            # ROUTER_CONTROL_PERSIST=0 if a given dropbear build misbehaves with multiplexing.
            argv += [
                "-o", "ControlMaster=auto",
                "-o", "ControlPath=/tmp/mage-ssh-%r@%h:%p.sock",
                "-o", f"ControlPersist={self.control_persist}",
            ]
        # Prepend the Merlin PATH (dropbear strips the env — see above) so bare tool names resolve.
        return argv + [f"{self.user}@{self.host}", "--", f"{_MERLIN_PATH} {remote_cmd}"]

    def run(self, argv: list[str], timeout: int = 60, cap: int | None = None) -> dict:
        # Rewrite a leading bare `sh` to the absolute remote shell so `sh -c ...` payloads don't
        # resolve to Broadcom's `sh` memory-tool applet on _MERLIN_PATH (see _DEFAULT_REMOTE_SHELL).
        if argv and argv[0] == "sh":
            argv = [self.remote_shell, *argv[1:]]
        res = _exec(self._ssh_argv(shlex.join(argv)), timeout, self.cap if cap is None else cap)
        # ssh exit 255 == TRANSPORT failure (auth/route/disconnect), distinct from the remote
        # command's own rc. Disruptive ops (reboot, restart_wan/wireless/firewall) can drop the
        # session mid-command: flag it so callers/audit don't read a transport drop as a clean
        # failure — such a result is INDETERMINATE (the command may have applied).
        if res.get("rc") == 255:
            res["transport_error"] = True
        return res

    @classmethod
    def from_env(cls, cap: int = DEFAULT_OUTPUT_CAP) -> "SSHRunner":
        """Build from ROUTER_* env (see router-hands/.env.example). ROUTER_HOST is required."""
        host = os.environ.get("ROUTER_HOST")
        if not host:
            raise SystemExit("ROUTER_HOST is required for the SSH relay (set it in .env)")
        strict = os.environ.get("ROUTER_STRICT_HOST_KEY", "yes")
        known_hosts = os.environ.get("ROUTER_KNOWN_HOSTS", "/secrets/known_hosts") or None
        # Footgun guard: accept-new is a first-deploy bootstrap. Leaving it on once the host key is
        # pinned silently re-enables TOFU (a LAN MITM could impersonate the router). Warn loudly.
        if (
            strict == "accept-new"
            and known_hosts
            and os.path.exists(known_hosts)
            and os.path.getsize(known_hosts) > 0
        ):
            print(
                f"[mage-hands] WARNING: ROUTER_STRICT_HOST_KEY=accept-new but {known_hosts} is "
                f"already populated — host-key pinning is effectively disabled. Set it to 'yes'.",
                file=sys.stderr,
                flush=True,
            )
        # The inverse footgun: strict pinning with no pinned key means EVERY ssh call fails
        # with a cryptic host-key error at tool time. Fail fast at startup instead.
        if strict == "yes" and (
            not known_hosts
            or not os.path.exists(known_hosts)
            or os.path.getsize(known_hosts) == 0
        ):
            raise SystemExit(
                f"ROUTER_STRICT_HOST_KEY=yes but the known_hosts file "
                f"({known_hosts or 'ROUTER_KNOWN_HOSTS unset'}) is missing or empty — every SSH "
                f"call would fail host-key verification. Pin the router's key first (e.g. "
                f"`ssh-keyscan -p <port> <router>` >> that file; see router-hands/README.md), or "
                f"bootstrap a first deploy with ROUTER_STRICT_HOST_KEY=accept-new."
            )
        return cls(
            host=host,
            user=os.environ.get("ROUTER_USER", "admin"),
            port=int(os.environ.get("ROUTER_PORT", "22")),
            key_file=os.environ.get("ROUTER_SSH_KEY", "/secrets/router_key"),
            connect_timeout=int(os.environ.get("ROUTER_CONNECT_TIMEOUT", "10")),
            strict_host_key_checking=strict,
            known_hosts=known_hosts,
            control_persist=int(os.environ.get("ROUTER_CONTROL_PERSIST", "60")),
            remote_shell=os.environ.get("ROUTER_REMOTE_SHELL", _DEFAULT_REMOTE_SHELL),
            cap=cap,
        )


# ── Catastrophic-command denylist ────────────────────────────────────────────────────────────
# CONTRACT (read before editing):
#   • Each entry is a regex matched with ``re.search`` against the RAW, flattened ``command``
#     string (the single arg the caller passes to run(), later handed to ``sh -c``). Matching is
#     NOT tokenized and NOT path-anchored.
#   • This is a best-effort BACKSTOP, not a guarantee. A determined ``echo reboot | sh`` can
#     evade it; the real controls are the two-call exec_token gate, ephemerality, and the audit.
#   • The availability patterns deliberately key on COMMAND POSITION (start, after a |;& shell
#     separator, after ``sudo``, or after a ``/`` path prefix) so genuine invocations
#     (``reboot``, ``sudo reboot``, ``/sbin/reboot -f``) are refused while read-only inspection
#     that merely mentions the word (``last reboot``, ``grep reboot /var/log/...``) is allowed.
#   • Operators may ADD patterns via the RUN_DENY_EXTRA env var; those are APPENDED here, never
#     substituted (see synology-hands/server.py and config.py).
_CMD = r"(?:^|[|;&]|\bsudo\s+|/)\s*"   # command-position prefix
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
    # Availability backstop (command-position; see CONTRACT above). Catches reboot/shutdown/
    # poweroff/halt as an invocation (incl. `sudo reboot`, `/sbin/poweroff`) plus the systemctl
    # subcommand form, DSM's synopoweroff, `init 0`, LVM teardown, and killing PID 1 / -1.
    _CMD + r"(?:reboot|shutdown|poweroff|halt)\b",
    # Quoted/direct `sh -c` payload form of the availability backstop: the command-position
    # patterns above can't see inside the payload string. Narrow on purpose (the keyword must
    # OPEN the payload) so `grep 'reboot' ...`, `last reboot`, `sh -c 'last reboot'` stay
    # allowed. (`\bsh` cannot match inside `ssh` — no word boundary between the two s's.)
    r"\bsh\s+-c\s+[\"']?\s*(?:reboot|shutdown|poweroff|halt)\b",
    r"\bsystemctl\b[^|;&]*\b(?:reboot|poweroff|halt|kexec)\b",
    r"\bsynopoweroff\b",
    r"\binit\s+0\b",
    r"\b(?:lvremove|vgremove)\b",
    r"\bkill\b[^|&;]*\s-?1(?:\s|$)",
]


def register_run_tool(
    mcp,
    runner: Runner,
    *,
    ttl: int = 300,
    timeout: int = 300,
    deny_patterns: list[str] | None = None,
    output_cap: int = DEFAULT_OUTPUT_CAP,
):
    deny = [re.compile(p) for p in (deny_patterns or DEFAULT_DENY)]
    pending: dict[str, tuple[str, float]] = {}

    @mcp.tool(annotations={"destructiveHint": True})
    def run(
        command: Annotated[str, Field(
            description="POSIX shell command line, executed via `sh -c` as ROOT on the target "
                        "host, e.g. 'docker ps --format \"{{.Names}}\"'. Must be byte-identical "
                        "between the dry-run call and the exec_token replay."
        )],
        exec_token: Annotated[
            str | None,
            Field(description="One-time token returned by the prior dry-run call (the "
                              "'exec_token' field, an opaque url-safe string). Omit to dry-run; "
                              "pass it back with the identical command to execute."),
        ] = None,
        max_bytes: Annotated[
            int | None,
            Field(description="Optional byte cap on returned stdout/stderr, e.g. 4096. Only "
                              "NARROWS the server cap, never raises it; omit for the default."),
        ] = None,
    ) -> dict:
        """Tier C — arbitrary root command on the host. Last resort: prefer a dedicated Tier-A
        inspection tool or read_file when one covers the need.

        Call once WITHOUT exec_token to get a dry-run preview plus a one-time token, then call
        again replaying that exact token (same command) to execute. Catastrophic patterns are
        refused outright. Returns {dry_run, would_run, exec_token, ttl_seconds} on the first
        call and {rc, stdout, stderr} (truncated at the output cap) on execution.
        """
        # GC expired dry-run tokens — otherwise un-replayed dry-runs accumulate forever.
        # Cardinality stays tiny (one entry per un-replayed dry-run in an ephemeral, mostly
        # single-user relay), so a full scan per call is fine.
        now = time.time()
        for tok in [t for t, (_, exp) in pending.items() if exp < now]:
            pending.pop(tok, None)

        if any(p.search(command) for p in deny):
            return {
                "refused": True,
                "parameter": "command",
                "reason": "'command' matches the catastrophic-pattern denylist (destructive/"
                          "availability operation); it will not run even with an exec_token",
            }

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
                "parameter": "exec_token",
                "reason": "'exec_token' is invalid/expired, or 'command' differs from the "
                          "dry-run; call run() again without exec_token for a fresh dry-run",
            }

        # Per-call cap can only narrow the server cap, never exceed it.
        cap = output_cap if max_bytes is None else min(max_bytes, output_cap)
        if cap < 1:
            cap = output_cap
        return runner.run(["sh", "-c", command], timeout=timeout, cap=cap)

    run._pending = pending  # test/observability hook (pending is otherwise closure-local)
    return run
