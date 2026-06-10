"""Unit tests for PathPolicy + fs_reader, focused on the symlink-resolution re-check: a
relative symlink under an allowed root must not dodge the allow/deny lists.

Run from the common/ project dir:  uv run --with pytest --with fastmcp pytest tests -q
"""

from __future__ import annotations

import pytest

from mage_hands_core.policy import PathPolicy, fs_reader, register_read_file


class FakeMCP:
    """Minimal stand-in: @mcp.tool(...) just returns the function unchanged."""

    def tool(self, *a, **k):
        def deco(fn):
            return fn
        return deco


POLICY = PathPolicy(allow=["/volume1"], deny=["/etc/shadow"])


@pytest.fixture
def host(tmp_path):
    """A fake /host mount: allowed /volume1 plus an /etc with a 'secret'."""
    base = tmp_path / "host"
    (base / "volume1").mkdir(parents=True)
    (base / "etc").mkdir()
    (base / "volume1" / "real.txt").write_text("benign")
    (base / "etc" / "shadow").write_text("root:hash")
    (base / "etc" / "passwd").write_text("root:x")
    return base


def make_reader(base):
    return fs_reader(str(base), policy=POLICY)


def test_benign_file_reads(host):
    assert make_reader(host)("/volume1/real.txt") == "benign"


def test_benign_in_root_symlink_reads(host):
    (host / "volume1" / "ok").symlink_to(host / "volume1" / "real.txt")
    assert make_reader(host)("/volume1/ok") == "benign"


def test_relative_symlink_to_denied_path_refused(host):
    # The verified bypass: lexical policy.check saw only /volume1/link; the resolved target
    # (/host/etc/shadow) passed bare containment. The re-check must refuse it.
    (host / "volume1" / "link").symlink_to("../etc/shadow")
    with pytest.raises(PermissionError):
        make_reader(host)("/volume1/link")


def test_relative_symlink_out_of_allowed_roots_refused(host):
    (host / "volume1" / "link2").symlink_to("../etc/passwd")  # not denied, but not allowed
    with pytest.raises(PermissionError):
        make_reader(host)("/volume1/link2")


def test_absolute_symlink_out_of_host_blocked(host, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("escape")
    (host / "volume1" / "abs").symlink_to(outside)
    with pytest.raises(ValueError, match="path traversal blocked"):
        make_reader(host)("/volume1/abs")


def test_end_to_end_via_register_read_file(host):
    read_file = register_read_file(FakeMCP(), POLICY, make_reader(host))
    assert read_file("/volume1/real.txt")["content"] == "benign"
    (host / "volume1" / "link").symlink_to("../etc/shadow")
    with pytest.raises(PermissionError):
        read_file("/volume1/link")
