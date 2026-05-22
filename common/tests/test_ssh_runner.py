"""Unit tests for the SSH transport additions (router-hands): SSHRunner + runner_reader.

Run from the common/ project dir:  uv run --with pytest --with fastmcp pytest tests -q
"""

from __future__ import annotations

import pytest

import mage_hands_core.exec as ex
from mage_hands_core.exec import SSHRunner, _MERLIN_PATH
from mage_hands_core.policy import runner_reader


# ── _ssh_argv: quoting, PATH prefix, options ───────────────────────────────────────────────────

def test_ssh_argv_quoting_and_path():
    r = SSHRunner(host="r.lan", user="admin", port=2222, key_file="/k",
                  known_hosts="/kh", control_persist=60)
    argv = r._ssh_argv("sh -c 'iptables -L'")
    assert argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == "/k"
    assert argv[argv.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=/kh" in argv
    assert "ControlMaster=auto" in argv
    assert argv[-3] == "admin@r.lan"
    assert argv[-2] == "--"
    # exactly one remote string, PATH-prefixed (dropbear strips the env), command preserved
    assert argv[-1] == f"{_MERLIN_PATH} sh -c 'iptables -L'"


def test_ssh_argv_control_persist_zero_disables_mux():
    argv = SSHRunner(host="x", control_persist=0)._ssh_argv("y")
    assert not any("ControlMaster" in a for a in argv)


def test_ssh_argv_known_hosts_none_omits_option():
    argv = SSHRunner(host="x", known_hosts=None)._ssh_argv("y")
    assert not any("UserKnownHostsFile" in a for a in argv)


# ── run(): quoting round-trip, cap/timeout threading, transport_error ───────────────────────────

def test_run_quotes_argv_and_threads_cap_timeout(monkeypatch):
    captured = {}

    def fake(argv, timeout, cap):
        captured.update(argv=argv, timeout=timeout, cap=cap)
        return {"rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(ex, "_exec", fake)
    SSHRunner(host="r.lan", cap=123).run(["sh", "-c", "iptables -L"], timeout=42)
    assert captured["argv"][-1].endswith("sh -c 'iptables -L'")
    assert captured["timeout"] == 42
    assert captured["cap"] == 123                      # runner's own cap when none passed
    SSHRunner(host="r.lan", cap=123).run(["cat", "/a b"], cap=7)
    assert captured["argv"][-1].endswith("cat '/a b'")  # path with space round-trips
    assert captured["cap"] == 7                         # per-call cap override


def test_run_flags_transport_error_on_255(monkeypatch):
    monkeypatch.setattr(ex, "_exec", lambda a, t, c: {"rc": 255, "stdout": "", "stderr": "no route"})
    assert SSHRunner(host="x").run(["true"]).get("transport_error") is True
    monkeypatch.setattr(ex, "_exec", lambda a, t, c: {"rc": 0, "stdout": "", "stderr": ""})
    assert "transport_error" not in SSHRunner(host="x").run(["true"])
    monkeypatch.setattr(ex, "_exec", lambda a, t, c: {"rc": 1, "stdout": "", "stderr": "bad"})
    assert "transport_error" not in SSHRunner(host="x").run(["false"])  # rc 1 is a command error, not transport


# ── from_env: required host, defaults, accept-new footgun guard ─────────────────────────────────

def test_from_env_requires_host(monkeypatch):
    monkeypatch.delenv("ROUTER_HOST", raising=False)
    with pytest.raises(SystemExit):
        SSHRunner.from_env()


def test_from_env_defaults(monkeypatch):
    monkeypatch.setenv("ROUTER_HOST", "r.lan")
    for k in ("ROUTER_USER", "ROUTER_PORT", "ROUTER_SSH_KEY", "ROUTER_KNOWN_HOSTS",
              "ROUTER_STRICT_HOST_KEY", "ROUTER_CONNECT_TIMEOUT", "ROUTER_CONTROL_PERSIST"):
        monkeypatch.delenv(k, raising=False)
    r = SSHRunner.from_env(cap=99)
    assert (r.host, r.user, r.port) == ("r.lan", "admin", 22)
    assert r.key_file == "/secrets/router_key"
    assert r.known_hosts == "/secrets/known_hosts"
    assert r.strict_host_key_checking == "yes"
    assert r.control_persist == 60
    assert r.cap == 99


def test_from_env_accept_new_warns_when_known_hosts_populated(monkeypatch, tmp_path, capsys):
    kh = tmp_path / "known_hosts"
    kh.write_text("r.lan ssh-ed25519 AAAA\n")
    monkeypatch.setenv("ROUTER_HOST", "r.lan")
    monkeypatch.setenv("ROUTER_STRICT_HOST_KEY", "accept-new")
    monkeypatch.setenv("ROUTER_KNOWN_HOSTS", str(kh))
    SSHRunner.from_env()
    err = capsys.readouterr().err
    assert "WARNING" in err and "accept-new" in err


def test_from_env_accept_new_quiet_when_known_hosts_empty(monkeypatch, tmp_path, capsys):
    kh = tmp_path / "known_hosts"
    kh.write_text("")  # size 0 → genuine first-deploy bootstrap, no warning
    monkeypatch.setenv("ROUTER_HOST", "r.lan")
    monkeypatch.setenv("ROUTER_STRICT_HOST_KEY", "accept-new")
    monkeypatch.setenv("ROUTER_KNOWN_HOSTS", str(kh))
    SSHRunner.from_env()
    assert "WARNING" not in capsys.readouterr().err


# ── runner_reader: read over the Runner, cap, error mapping ─────────────────────────────────────

class _Reader:
    def __init__(self, rc, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr, self.calls = rc, stdout, stderr, []

    def run(self, argv, timeout=60, cap=None):
        self.calls.append({"argv": argv, "cap": cap})
        return {"rc": self.rc, "stdout": self.stdout, "stderr": self.stderr}


def test_runner_reader_reads_and_passes_cap():
    rd = _Reader(0, stdout="hello\n")
    assert runner_reader(rd, max_bytes=200_000)("/tmp/x") == "hello\n"
    assert rd.calls[0]["argv"] == ["cat", "/tmp/x"]
    assert rd.calls[0]["cap"] == 200_000          # read cap, not the runner's default command cap


def test_runner_reader_missing_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        runner_reader(_Reader(1, stderr="cat: no such file"))("/tmp/missing")
