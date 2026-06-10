"""Unit tests for the mage_hands_core security/tooling changes.

Run from the repo root:  uv run --with pytest --with fastmcp pytest common/tests -q
"""

from __future__ import annotations

import pytest

from mage_hands_core.config import Config, _split_list
from mage_hands_core.exec import (
    DEFAULT_DENY,
    DEFAULT_OUTPUT_CAP,
    ShellRunner,
    register_run_tool,
)


# ── test doubles ──────────────────────────────────────────────────────────────────────────────

class FakeMCP:
    """Minimal stand-in: @mcp.tool(...) just returns the function unchanged."""

    def tool(self, *a, **k):
        def deco(fn):
            return fn
        return deco


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, timeout=60, cap=None):
        self.calls.append({"argv": argv, "timeout": timeout, "cap": cap})
        return {"rc": 0, "stdout": "ok", "stderr": ""}


def make_run(output_cap=DEFAULT_OUTPUT_CAP, extra=None, ttl=300):
    runner = FakeRunner()
    run = register_run_tool(
        FakeMCP(), runner,
        ttl=ttl,
        deny_patterns=DEFAULT_DENY + (extra or []),
        output_cap=output_cap,
    )
    return run, runner


# ── denylist ────────────────────────────────────────────────────────────────────────────────

REFUSED = [
    # pre-existing catastrophic
    "rm -rf /", "rm -rf /*", "rm -rf /volume1", "rm -rf /volume1/", "rm -rf /volume1/*",
    "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", "parted /dev/sda",
    "mdadm --zero-superblock /dev/sda", "synostorage --delete pool",
    # new availability backstop
    "reboot", "reboot now", "sudo reboot", "/sbin/reboot -f", "shutdown -h now", "poweroff",
    "halt", "systemctl reboot", "systemctl --force poweroff", "synopoweroff", "init 0",
    "lvremove /dev/vg0/lv0", "vgremove vg0", "kill -9 1", "kill -1", "kill -s KILL 1",
    # `sh -c` payload forms (quoted, unquoted, odd spacing) of the availability backstop
    "sh -c 'reboot'", 'sh -c "shutdown -h now"', "/bin/sh -c 'poweroff'",
    "busybox sh -c 'halt'", "sh -c reboot", "sh    -c    reboot", "sh -c    'shutdown now'",
]

ALLOWED = [
    # mentions a dangerous word but is read-only / targeted work → must be tokenized, not refused
    "last reboot", "grep reboot /var/log/messages", "journalctl | grep poweroff",
    "cat /etc/synoinfo.conf", "rm -rf /volume1/docker/app/cache", "kill -9 12345",
    "docker ps -a", "synogetkeyvalue /etc/synoinfo.conf ddns_update",
    # the keyword merely INSIDE an `sh -c` payload (not opening it) stays allowed
    "sh -c 'last reboot'", "sh -c 'grep reboot /var/log/messages'",
]


@pytest.mark.parametrize("cmd", REFUSED)
def test_denylist_refuses(cmd):
    run, runner = make_run()
    res = run(cmd)
    assert res.get("refused") is True, f"should be refused: {cmd!r}"
    assert runner.calls == [], "runner must never be invoked for a refused command"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_denylist_allows(cmd):
    run, _ = make_run()
    res = run(cmd)
    assert res.get("dry_run") is True, f"should be tokenized (allowed): {cmd!r}"


# ── two-call gate + cap threading ─────────────────────────────────────────────────────────────

def test_dry_run_then_replay_executes():
    run, runner = make_run(output_cap=100)
    dry = run("rm -rf /volume1/docker/app/cache")
    assert dry["dry_run"] is True
    res = run("rm -rf /volume1/docker/app/cache", exec_token=dry["exec_token"])
    assert res["rc"] == 0
    assert len(runner.calls) == 1
    assert runner.calls[0]["cap"] == 100  # default server cap when no max_bytes


def test_replayed_token_is_single_use():
    run, runner = make_run()
    dry = run("echo hi")
    run("echo hi", exec_token=dry["exec_token"])
    again = run("echo hi", exec_token=dry["exec_token"])
    assert again.get("refused") is True
    assert len(runner.calls) == 1


def test_max_bytes_only_narrows_never_raises():
    run, runner = make_run(output_cap=100)

    dry = run("echo hi")
    run("echo hi", exec_token=dry["exec_token"], max_bytes=10)
    assert runner.calls[-1]["cap"] == 10  # narrowed

    dry = run("echo hi")
    run("echo hi", exec_token=dry["exec_token"], max_bytes=10**9)
    assert runner.calls[-1]["cap"] == 100  # clamped to server cap, never exceeds it


def test_expired_pending_tokens_are_garbage_collected(monkeypatch):
    import mage_hands_core.exec as ex

    run, _ = make_run(ttl=300)
    run("echo a")
    run("echo b")
    assert len(run._pending) == 2

    real_time = ex.time.time
    monkeypatch.setattr(ex.time, "time", lambda: real_time() + 301)
    run("echo c")  # any call sweeps the expired entries
    assert len(run._pending) == 1  # only echo-c's fresh token survives


def test_shellrunner_truncates_at_cap():
    r = ShellRunner(cap=5)
    res = r.run(["printf", "%s", "abcdefghij"])
    assert res["stdout"].startswith("abcde")
    assert "truncated" in res["stdout"]
    # per-call cap override narrows further
    res2 = r.run(["printf", "%s", "abcdefghij"], cap=3)
    assert res2["stdout"].startswith("abc")
    assert "truncated" in res2["stdout"]


# ── Config: env-overridable policy ────────────────────────────────────────────────────────────

def test_split_list_set_but_empty_is_noop():
    assert _split_list(None) == []
    assert _split_list("") == []
    assert _split_list("a, b\nc") == ["a", "b", "c"]


def test_output_cap_clamped_to_max(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "x" * 32)
    monkeypatch.setenv("OUTPUT_CAP", "999999999")
    monkeypatch.setenv("OUTPUT_CAP_MAX", "1000")
    cfg = Config.from_env()
    assert cfg.output_cap == 1000


def test_short_relay_token_fails_at_load(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "x" * 15)
    with pytest.raises(SystemExit):
        Config.from_env()


def test_malformed_run_deny_extra_fails_at_load(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "x" * 32)
    monkeypatch.setenv("RUN_DENY_EXTRA", "[unclosed")
    with pytest.raises(SystemExit):
        Config.from_env()


def test_read_policy_extras_parse(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "x" * 32)
    monkeypatch.setenv("READ_ALLOW_EXTRA", "/volume2,/extra")
    monkeypatch.setenv("READ_DENY_EXTRA", "")  # set-but-empty → no-op
    monkeypatch.setenv("READ_POLICY_OVERRIDE", "yes")
    cfg = Config.from_env()
    assert cfg.read_allow_extra == ["/volume2", "/extra"]
    assert cfg.read_deny_extra == []
    assert cfg.read_policy_override is True


def test_run_deny_extra_is_additive(monkeypatch):
    # extra pattern refuses a new command, while built-ins still apply
    run, runner = make_run(extra=[r"\bcurl\b.*\bevil\.com\b"])
    assert run("curl http://evil.com/x").get("refused") is True
    assert run("rm -rf /").get("refused") is True
    assert run("curl http://good.com/x").get("dry_run") is True
