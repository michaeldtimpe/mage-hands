"""Offline, deterministic unit tests for router-hands' pure parsers + the import-time secret-key
guard. No router needed — a FakeHost canned-responds to the executor protocol so the nvram/firmware/
exposure helpers can be exercised without SSH.

Run from router-hands/:
    uv run --with pytest --with fastmcp --with ../common pytest tests -q
"""

import server
from server import (
    _conntrack,
    _flag,
    _internet_exposure,
    _iowait_delta,
    _nvram_many,
    _parse_listeners,
    _parse_upnp_leases,
    _parse_vts_rulelist,
    _pending_updates,
)

OUT = lambda r: (r.get("stdout") or "").strip()  # noqa: E731 — mirrors server.py's main() `out`


class FakeHost:
    """Minimal Runner stand-in. nvram: key->value (missing key = '' = the rc-0-empty trap).
    files: path->stdout for cat/ls. cmds: substring(joined argv)->response dict override."""

    def __init__(self, nvram=None, files=None, cmds=None):
        self.nvram = nvram or {}
        self.files = files or {}
        self.cmds = cmds or {}

    def run(self, argv, timeout=60, cap=None):
        joined = " ".join(argv)
        for key, resp in self.cmds.items():
            if key in joined:
                return {"rc": 0, "stdout": "", "stderr": "", **resp}
        if argv[:2] == ["sh", "-c"] and len(argv) >= 3 and argv[2].startswith("for k in "):
            keys = argv[2][len("for k in "):].split(";")[0].split()
            return {"rc": 0, "stdout": "\n".join(f"{k}={self.nvram.get(k, '')}" for k in keys),
                    "stderr": ""}
        if argv[:2] == ["nvram", "get"] and len(argv) >= 3:
            return {"rc": 0, "stdout": self.nvram.get(argv[2], ""), "stderr": ""}
        if argv[:1] == ["cat"]:
            return {"rc": 0 if argv[1] in self.files else 1,
                    "stdout": self.files.get(argv[1], ""), "stderr": ""}
        if argv[:1] == ["ls"]:
            return {"rc": 0 if argv[1] in self.files else 1, "stdout": "", "stderr": ""}
        return {"rc": 0, "stdout": "", "stderr": ""}


# ── _parse_vts_rulelist ──────────────────────────────────────────────────────────────────────

def test_vts_rulelist_5_and_6_fields():
    raw = "<Web>8080>192.168.1.10>80>TCP<SSH>2222>192.168.1.11>22>TCP>203.0.113.5"
    rules = _parse_vts_rulelist(raw)
    assert len(rules) == 2
    assert rules[0] == {"name": "Web", "ext_port": "8080", "int_ip": "192.168.1.10",
                        "int_port": "80", "proto": "TCP", "src_restrict": None}
    # 6th field present → source restriction surfaced (materially lowers exposure)
    assert rules[1]["src_restrict"] == "203.0.113.5"


def test_vts_rulelist_empty_and_trailing():
    assert _parse_vts_rulelist("") == []
    # trailing/empty records (stale NVRAM tail) must not index-error
    rules = _parse_vts_rulelist("<Web>8080>10.0.0.2>80>TCP<")
    assert len(rules) == 1 and rules[0]["name"] == "Web"


def test_vts_rulelist_short_record_no_indexerror():
    rules = _parse_vts_rulelist("<Partial>1234")
    assert rules[0]["int_ip"] is None and rules[0]["proto"] is None


# ── _parse_upnp_leases / _parse_listeners ────────────────────────────────────────────────────

def test_parse_upnp_leases():
    leases = _parse_upnp_leases("TCP:51000:192.168.1.50:51000:0:torrent\nweird-line")
    assert leases[0] == {"proto": "TCP", "ext_port": "51000", "int_ip": "192.168.1.50",
                         "int_port": "51000"}
    assert leases[1] == {"raw": "weird-line"}


def test_parse_listeners_wildcard_and_udp():
    raw = (
        "Proto Recv-Q Send-Q Local Address Foreign Address State PID/Program name\n"
        "tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN 123/dropbear\n"
        "tcp 0 0 192.168.1.1:80 0.0.0.0:* LISTEN 456/httpd\n"        # bound to LAN-only IP, skip
        "udp 0 0 0.0.0.0:51820 0.0.0.0:* 789/wireguard\n"            # UDP listener (WireGuard)
        "tcp 0 0 :::443 :::* LISTEN -\n"                              # IPv6 wildcard, no program
    )
    got = _parse_listeners(raw)
    ports = {(l["proto"], l["port"]) for l in got}
    assert ("tcp", "22") in ports
    assert ("udp", "51820") in ports          # UDP must be captured
    assert ("tcp", "443") in ports            # IPv6 wildcard captured
    assert ("tcp", "80") not in ports         # LAN-bound listener excluded
    progs = {l["port"]: l["program"] for l in got}
    assert progs["22"] == "123/dropbear" and progs["443"] is None


# ── _flag (empty = unknown, never disabled) ──────────────────────────────────────────────────

def test_flag_empty_is_unknown_not_disabled():
    assert _flag("") == (None, "unknown")
    assert _flag("1") == (True, "direct_config")
    assert _flag("0") == (False, "direct_config")


# ── _iowait_delta ────────────────────────────────────────────────────────────────────────────

def test_iowait_delta():
    text = ("cpu  100 0 100 700 100 0 0 0\nother\n---\ncpu  110 0 110 760 130 0 0 0\n")
    # deltas: user10 nice0 sys10 idle60 iowait30 → total=110, iowait=30 → 27.3%
    assert _iowait_delta(text) == 27.3


def test_iowait_delta_malformed():
    assert _iowait_delta("garbage") == "unavailable"
    assert _iowait_delta("cpu 1 2\n---\ncpu 3 4") == "unavailable"  # <5 fields


# ── _conntrack (pressure band; both-empty = unavailable, not null) ───────────────────────────

def test_conntrack_pressure_band():
    host = FakeHost(cmds={"nf_conntrack": {"stdout":
        "/proc/sys/net/netfilter/nf_conntrack_count=600\n"
        "/proc/sys/net/netfilter/nf_conntrack_max=1000"}})
    ct = _conntrack(host)
    assert ct["used"] == 600 and ct["max"] == 1000 and ct["pct"] == 60.0
    assert ct["pressure"] == "moderate"


def test_conntrack_both_empty_is_unavailable():
    ct = _conntrack(FakeHost())  # nothing matches → empty stdout
    assert ct["status"] == "unavailable" and "module" in ct["note"]


# ── _nvram_many (batched k=value parse) ──────────────────────────────────────────────────────

def test_nvram_many():
    host = FakeHost(nvram={"a": "1", "b": ""})
    assert _nvram_many(host, ("a", "b", "c")) == {"a": "1", "b": "", "c": ""}


# ── _pending_updates mapping ─────────────────────────────────────────────────────────────────

def test_pending_updates_available_true():
    host = FakeHost(nvram={"webs_state_flag": "1", "webs_state_update": "1",
                           "webs_state_info": "3004_388_8_4", "firmver": "3.0.0.4"})
    res = _pending_updates(host, OUT, check=False)
    assert res["firmware"]["available"] is True
    assert res["firmware"]["available_version"] == "3004_388_8_4"


def test_pending_updates_available_false():
    host = FakeHost(nvram={"webs_state_flag": "0", "webs_state_update": "1"})
    assert _pending_updates(host, OUT, check=False)["firmware"]["available"] is False


def test_pending_updates_unknown_when_never_checked():
    res = _pending_updates(FakeHost(), OUT, check=False)  # all empty
    fw = res["firmware"]
    assert fw["available"] is None and fw["confidence"] == "unknown"


# ── _internet_exposure (empty = unknown discipline + populated parse) ────────────────────────

def test_internet_exposure_empty_is_unknown():
    res = _internet_exposure(FakeHost(), OUT)  # all nvram empty, no leases, no listeners
    assert res["ssh"]["enabled"] is None and res["ssh"]["scope"] == "unknown"
    assert res["dmz"]["enabled"] is None
    assert res["remote_admin"]["enabled"] is None
    assert res["remote_admin"]["confidence"] == "unknown"
    assert res["ipv6"]["firewall_enabled"] is None
    assert res["port_forwards"]["count"] == 0


def test_internet_exposure_ssh_nonzero_is_ambiguous_not_lan():
    res = _internet_exposure(FakeHost(nvram={"sshd_enable": "1"}), OUT)
    # nonzero must NOT be assumed LAN — false-negative on WAN SSH is worse than false-positive
    assert res["ssh"]["enabled"] is True
    assert res["ssh"]["scope"] == "unknown (verify)"
    assert res["ssh"]["confidence"] == "ambiguous_vendor_semantics"


def test_internet_exposure_populated():
    host = FakeHost(
        nvram={"dmz_ip": "192.168.1.5", "vts_enable_x": "1",
               "vts_rulelist": "<Web>8080>192.168.1.10>80>TCP", "upnp_enable": "1"},
        files={"/tmp/upnp.leases": "TCP:51000:192.168.1.50:51000:0:bt"},
    )
    res = _internet_exposure(host, OUT)
    assert res["dmz"]["enabled"] is True and res["dmz"]["host"] == "192.168.1.5"
    assert res["port_forwards"]["count"] == 1
    assert res["port_forwards"]["rules"][0]["int_ip"] == "192.168.1.10"
    assert res["upnp"]["confidence"] == "runtime_observed"
    assert res["upnp"]["active_mappings"][0]["ext_port"] == "51000"


def test_internet_exposure_upnp_file_absent_is_not_off():
    # config enabled but no leases file → enabled True with a 'no active mappings' detail, NOT off
    res = _internet_exposure(FakeHost(nvram={"upnp_enable": "1"}), OUT)
    assert res["upnp"]["config_enabled"] is True
    assert res["upnp"]["active_mappings"] == []
    assert "no active mappings" in res["upnp"]["detail"]


# ── the most important test: the secret-key guard must actually fire ──────────────────────────

def test_secret_key_guard_is_clean_for_real_allowlist():
    assert server._FORBIDDEN_EXPOSURE_KEYS == []


def test_secret_key_guard_catches_real_secret_keys():
    for bad in ("http_passwd", "wl0_wpa_psk", "ddns_passwd", "vpn_crt_server1_ca",
                "vpn_crt_server1_key", "wgs_priv"):
        assert server._SECRET_KEY_RE.search(bad), f"guard failed to flag {bad}"


def test_secret_key_guard_fires_when_forbidden_key_injected():
    # prove the guard is not a silent no-op: injecting a secret key into the allowlist is detected
    injected = server._EXPOSURE_NVRAM_KEYS + ("http_passwd",)
    forbidden = [k for k in injected if server._SECRET_KEY_RE.search(k)]
    assert forbidden == ["http_passwd"]
