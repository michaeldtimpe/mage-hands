"""Unit tests for the DSM firewall pure logic (no live host needed).

The lock-out guard is the safety-critical piece — it is what stops firewall_set_rules from
stranding direct LAN admin access (the firewall can't cut the relay's loopback/tailnet path, but
it CAN cut a human's LAN SSH/DSM). Run from this directory:

    uv run --with pytest python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firewall import (  # noqa: E402
    _parse_webapi,
    _sample_ip,
    evaluate_access,
    lockout_guard,
    normalize_rule,
    validate_rule,
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

def allow_all_mgmt(source_group="all", source_ip="all"):
    return {"policy": "allow", "port_group": "reserved", "ports": "ssh,dms,dms_https",
            "protocol": "tcp", "source_ip_group": source_group, "source_ip": source_ip}

DROP_ALL = {"policy": "drop", "port_group": "all", "ports": "all", "protocol": "all",
            "source_ip_group": "all", "source_ip": "all"}


# ── validate_rule ──────────────────────────────────────────────────────────────────────────────

def test_validate_accepts_clean_rules():
    assert validate_rule(allow_all_mgmt()) == []
    assert validate_rule({"policy": "drop", "port_group": "all", "ports": "all",
                          "protocol": "all", "source_ip_group": "geoip", "source_ip": "RU,CN"}) == []
    assert validate_rule({"policy": "allow", "port_group": "custom", "ports": "8080,9000:9100",
                          "protocol": "tcp", "source_ip_group": "netmask",
                          "source_ip": "192.168.1.0/24"}) == []

@pytest.mark.parametrize("rule,needle", [
    ({"policy": "permit", "port_group": "all", "ports": "all", "source_ip_group": "all"}, "policy"),
    ({"policy": "allow", "port_group": "bogus", "ports": "x", "source_ip_group": "all"}, "port_group"),
    ({"policy": "allow", "port_group": "all", "ports": "all", "source_ip_group": "subnet"}, "source_ip_group"),
    ({"policy": "allow", "port_group": "all", "ports": "all",
      "source_ip_group": "netmask", "source_ip": "999.1.1.0/24"}, "CIDR"),
    ({"policy": "allow", "port_group": "all", "ports": "all", "source_ip_group": "ip",
      "source_ip": "not-an-ip"}, "not an IP"),
    ({"policy": "allow", "port_group": "service", "ports": "", "source_ip_group": "all"}, "non-empty"),
])
def test_validate_rejects_bad_rules(rule, needle):
    errs = validate_rule(rule)
    assert any(needle in e for e in errs), errs


# ── evaluate_access (first-match) ───────────────────────────────────────────────────────────────

def test_empty_or_unmatched_is_implicit_deny():
    assert evaluate_access([], "192.168.1.10", "ssh", 22) == "implicit_deny"
    only_web = [{"policy": "allow", "port_group": "custom", "ports": "443", "protocol": "tcp",
                 "source_ip_group": "all", "source_ip": "all", "enable": True}]
    assert evaluate_access(only_web, "192.168.1.10", "ssh", 22) == "implicit_deny"

def test_first_match_wins():
    rules = [allow_all_mgmt(), DROP_ALL]
    assert evaluate_access(rules, "192.168.1.10", "ssh", 22) == "allow"
    # a leading drop shadows a later allow
    assert evaluate_access([DROP_ALL, allow_all_mgmt()], "192.168.1.10", "ssh", 22) == "drop"

def test_disabled_rule_is_skipped():
    r = {**allow_all_mgmt(), "enable": False}
    assert evaluate_access([r], "192.168.1.10", "ssh", 22) == "implicit_deny"

def test_netmask_source_scoping():
    rules = [allow_all_mgmt("netmask", "192.168.1.0/24")]
    assert evaluate_access(rules, "192.168.1.50", "ssh", 22) == "allow"
    assert evaluate_access(rules, "10.0.0.5", "ssh", 22) == "implicit_deny"

def test_geoip_never_matches_private_ip():
    rules = [{"policy": "allow", "port_group": "all", "ports": "all", "protocol": "all",
              "source_ip_group": "geoip", "source_ip": "US", "enable": True}]
    assert evaluate_access(rules, "192.168.1.10", "ssh", 22) == "implicit_deny"

def test_custom_port_range_and_protocol():
    rules = [{"policy": "allow", "port_group": "custom", "ports": "20:30", "protocol": "tcp",
              "source_ip_group": "all", "source_ip": "all", "enable": True}]
    assert evaluate_access(rules, "1.2.3.4", "ssh", 22, "tcp") == "allow"
    assert evaluate_access(rules, "1.2.3.4", "ssh", 22, "udp") == "implicit_deny"  # proto mismatch


# ── lockout_guard ──────────────────────────────────────────────────────────────────────────────

def test_guard_passes_when_mgmt_allowed():
    g = lockout_guard([allow_all_mgmt(), DROP_ALL], "192.168.1.0/24")
    assert g["ok"] is True

def test_guard_fails_on_default_deny_without_allow():
    g = lockout_guard([DROP_ALL], "192.168.1.0/24")
    assert g["ok"] is False
    assert "SSH" in g["reason"] and "suggested_rule" in g

def test_guard_fails_when_only_ssh_allowed():
    ssh_only = {"policy": "allow", "port_group": "service", "ports": "ssh", "protocol": "tcp",
                "source_ip_group": "all", "source_ip": "all"}
    g = lockout_guard([ssh_only, DROP_ALL], "192.168.1.0/24")
    assert g["ok"] is False
    assert "DSM" in g["reason"]

def test_guard_respects_source_scope():
    # mgmt allow only for 10.0.0.0/24, but operator manages from 192.168.1.0/24 -> guard fails
    g = lockout_guard([allow_all_mgmt("netmask", "10.0.0.0/24"), DROP_ALL], "192.168.1.0/24")
    assert g["ok"] is False

def test_guard_refuses_geoip_management_source():
    g = lockout_guard([allow_all_mgmt()], "US")
    assert g["ok"] is False
    assert "geoip" in g["reason"] or "must be" in g["reason"]


# ── _sample_ip ─────────────────────────────────────────────────────────────────────────────────

def test_sample_ip_forms():
    assert _sample_ip("all") == "192.168.255.254"
    assert _sample_ip("192.168.1.5") == "192.168.1.5"
    assert _sample_ip("192.168.1.0/24") == "192.168.1.1"
    assert _sample_ip("10.0.0.1~10.0.0.9") == "10.0.0.1"
    with pytest.raises(ValueError):
        _sample_ip("US")


# ── normalize_rule ─────────────────────────────────────────────────────────────────────────────

def test_normalize_fills_defaults_and_drops_unknowns():
    n = normalize_rule({"policy": "allow", "port_group": "all", "source_ip_group": "all",
                        "bogus_key": 1})
    assert n["enable"] is True and n["protocol"] == "all" and n["ports"] == "all"
    assert n["port_direction"] == "destination" and n["source_ip"] == "all"
    assert "bogus_key" not in n


# ── _parse_webapi (strips the [Line NNN] preamble) ─────────────────────────────────────────────

def test_parse_webapi_extracts_trailing_json():
    raw = ('[Line 265] Not a json value: default\n'
           '[Line 295] Exec WebAPI:  api=X, param={"name":"default"}, runner=SYSTEM_ADMIN\n'
           '{\n   "data" : { "enable_firewall" : false },\n   "success" : true\n}\n')
    parsed = _parse_webapi(raw)
    assert parsed["success"] is True
    assert parsed["data"]["enable_firewall"] is False

def test_parse_webapi_handles_garbage():
    assert _parse_webapi("no json here").get("_parse_error") is True
