"""Unit tests for the audit helpers: byte-accurate truncate(), bounded args in the audit
line, and call-start activity touching.

Run from the common/ project dir:  uv run --with pytest --with fastmcp pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler

import pytest

import mage_hands_core.audit as audit_mod
from mage_hands_core.audit import ARGS_CAP, AuditMiddleware, truncate


@pytest.fixture(autouse=True)
def _fresh_audit_logger():
    # setup_audit reuses the process-wide "mage_hands.audit" logger; without a reset, every
    # middleware after the first would keep logging into the first test's tmp_path.
    log = logging.getLogger("mage_hands.audit")
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    yield


# ── truncate: byte semantics ──────────────────────────────────────────────────────────────────

def test_truncate_none_passthrough():
    assert truncate(None) is None


def test_truncate_ascii_under_at_over():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcde", 5) == "abcde"
    out = truncate("abcdefghij", 5)
    assert out.startswith("abcde")
    assert "<+5 bytes truncated>" in out


def test_truncate_counts_bytes_not_chars():
    # 100 chars but 200 UTF-8 bytes: under the old char-based slice this passed untouched.
    text = "é" * 100
    out = truncate(text, 150)
    assert out != text, "byte-oversized text must be truncated"
    assert "<+50 bytes truncated>" in out


def test_truncate_never_splits_a_multibyte_char():
    text = "é" * 100          # 2 bytes each
    out = truncate(text, 5)   # boundary lands mid-char
    head = out.split("...")[0]
    assert head == "é" * 2    # the half-cut 3rd char is dropped, not replaced
    assert "�" not in out
    out.encode("utf-8")       # round-trips cleanly


# ── AuditMiddleware ───────────────────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, name="run", arguments=None):
        self.message = _Msg(name, arguments)


def _drive(mw, ctx, call_next=None):
    async def default_next(_ctx):
        return "ok"

    return asyncio.run(mw.on_call_tool(ctx, call_next or default_next))


def _last_audit_line(tmp_path):
    return json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])


def test_oversized_args_logged_as_truncated_string(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "get_http_headers", lambda include=None: {})
    mw = AuditMiddleware(node_id="t", audit_dir=str(tmp_path))
    _drive(mw, _Ctx(arguments={"command": "x" * 100_000}))

    rec = _last_audit_line(tmp_path)
    assert isinstance(rec["args"], str)
    assert "truncated" in rec["args"]
    assert len(json.dumps(rec)) < ARGS_CAP + 1000  # the line itself is bounded


def test_small_args_stay_structured(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "get_http_headers", lambda include=None: {})
    mw = AuditMiddleware(node_id="t", audit_dir=str(tmp_path))
    _drive(mw, _Ctx(arguments={"path": "/etc/hosts"}))
    assert _last_audit_line(tmp_path)["args"] == {"path": "/etc/hosts"}


def test_setup_audit_writes_despite_a_foreign_handler(tmp_path):
    """A handler someone else attached must not suppress the audit file.

    The old `if not log.handlers` guard skipped creating the file handler whenever anything
    was already attached to `mage_hands.audit` — pytest's own capture handler was enough to
    do it — so calls were logged to nowhere with no error. Fail-open on a forensic log.
    """
    log = logging.getLogger("mage_hands.audit")
    log.addHandler(logging.NullHandler())  # stand-in for pytest's LogCaptureHandler

    audit_mod.setup_audit(str(tmp_path))
    log.info("probe")

    assert (tmp_path / "audit.jsonl").read_text().strip() == "probe"


def test_setup_audit_is_idempotent(tmp_path):
    """Repeat calls must not stack handlers (which would duplicate every audit line)."""
    log = logging.getLogger("mage_hands.audit")
    for _ in range(3):
        audit_mod.setup_audit(str(tmp_path))

    assert sum(isinstance(h, RotatingFileHandler) for h in log.handlers) == 1
    log.info("once")
    assert (tmp_path / "audit.jsonl").read_text().count("once") == 1


def test_setup_audit_retargets_to_a_new_dir(tmp_path):
    """Reconfiguring to a new dir must stop writing to the old one."""
    first, second = tmp_path / "a", tmp_path / "b"
    log = logging.getLogger("mage_hands.audit")

    audit_mod.setup_audit(str(first))
    log.info("to-first")
    audit_mod.setup_audit(str(second))
    log.info("to-second")

    assert "to-first" in (first / "audit.jsonl").read_text()
    assert "to-second" not in (first / "audit.jsonl").read_text()
    assert "to-second" in (second / "audit.jsonl").read_text()


def test_activity_touched_before_call(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "get_http_headers", lambda include=None: {})
    mw = AuditMiddleware(node_id="t", audit_dir=str(tmp_path))
    seen = {}

    async def call_next(_ctx):
        # last_activity must already exist while the (possibly long) call is in flight,
        # or the idle watchdog can tear the relay down mid-call.
        seen["touched"] = (tmp_path / "last_activity").exists()
        return "ok"

    _drive(mw, _Ctx(), call_next)
    assert seen["touched"] is True
