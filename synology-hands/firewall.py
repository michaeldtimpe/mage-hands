"""DSM firewall tools for synology-hands — audit, diagnose, and (guarded) mutation.

Everything here was reverse-engineered against a live DSM 7.2.1 box (kappa) rather than guessed,
because the firewall has the same "wrong oracle" traps that bit QuickConnect / the SSD cache (see
lessons.md). The empirical model:

  * ENABLE STATE has three corroborating oracles (all must agree before we call it):
      - ``synofirewall --info`` -> ``{"fw_enabled":0|1, ...}``  (DSM's own collector uses this)
      - webapi ``SYNO.Core.Security.Firewall get`` -> ``{"enable_firewall":bool,"profile_name":...}``
      - ``/usr/syno/etc/firewall.d/firewall_settings.json`` -> ``{"profile":...,"status":bool}``
    The key is NOT in /etc/synoinfo.conf (synogetkeyvalue returns rc 0 + empty there — the trap).
  * ENFORCEMENT is a *runtime* fact read from live iptables: when the firewall is actually applied,
    an ``INPUT_FIREWALL`` chain exists and INPUT jumps to it. "Configured enabled" and "actually
    enforced" are tracked separately so we can surface drift (enabled in config, never reloaded).
    NB: ``iptables -S INPUT`` (per-chain) errors on kappa's old kernel; ``iptables -S`` (whole
    table) works — so we always parse the full dump.
  * RULES have two encodings. The stored profile JSON (firewall.d/1.json, 2.json) and ``--info``
    use opaque integer codes (policy 0/1, ipGroup 5/3, ...). The *webapi* uses clean strings
    (policy allow/drop, source_ip_group all/ip/netmask/iprange/geoip, literal source_ip). We
    read AND write via the webapi so we never hand-encode the integer form. The webapi rule shape:
        {enable, name, policy(allow|drop), port_direction(destination|source),
         port_group(all|reserved|service|custom|system|self-defined), ports(csv|"all"),
         protocol(all|tcp|udp), source_ip_group(all|ip|netmask|iprange|geoip), source_ip}
    Round-trip: ``Profile get name=<p>`` -> edit ``data.global.rules`` -> ``Profile set`` with
    ``profile_applying:false`` (persists directly; the ``true`` variant is a staging two-phase
    commit that orphans a ``.test_<name>`` profile if its follow-up Apply fails) -> push live with
    ``synofirewall --reload`` only when ``<p>`` is the active profile and the firewall is enabled.

  * LOCK-OUT MODEL (load-bearing for the guard): Tailscale runs in USERSPACE on these boxes —
    there is no ``tailscale0`` interface; ingress is ``tailscale serve`` -> loopback. The generated
    ``INPUT_FIREWALL`` chain always begins ``-i lo -j ACCEPT`` + ``ESTABLISHED,RELATED -j ACCEPT``.
    So the relay's own MCP path (and any tailnet-sourced access, which also lands on loopback) can
    NEVER be cut by the firewall. The only real lock-out risk is DIRECT LAN access (the physical
    adapter, ovs_bond0) to SSH/DSM — the human's fallback. The guard protects exactly that.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Annotated

from pydantic import Field

# ── constants ─────────────────────────────────────────────────────────────────────────────────

FW_SETTINGS_PATH = "/usr/syno/etc/firewall.d/firewall_settings.json"

# Management services the lock-out guard insists stay reachable from the operator's LAN. Names are
# DSM firewall service keywords; ports are the canonical defaults (confirmed via `synofirewall
# --service`). SSH must stay open AND at least one DSM web port (5000 http / 5001 https).
MGMT_SSH = ("ssh", 22)
MGMT_DSM = (("dms", 5000), ("dms_https", 5001))

POLICIES = {"allow", "drop"}
PORT_GROUPS = {"all", "reserved", "service", "custom", "system", "self-defined"}
PROTOCOLS = {"all", "tcp", "udp"}
SOURCE_GROUPS = {"all", "ip", "netmask", "iprange", "geoip"}
PORT_DIRECTIONS = {"destination", "source"}


# ── webapi / CLI helpers ────────────────────────────────────────────────────────────────────────

def _parse_webapi(stdout: str) -> dict:
    """synowebapi prints ``[Line NNN] ...`` diagnostics before a pretty-printed JSON result block.

    The result is the last top-level object and always starts with a line that is exactly ``{``;
    parse from there (so a ``{`` inside the ``param={...}`` echo line can't fool us).
    """
    lines = (stdout or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "{":
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                break
    return {"_parse_error": True, "raw": (stdout or "")[:600]}


def webapi(host, api: str, method: str, version: int = 1, timeout: int = 30, **params) -> dict:
    """Call ``synowebapi --exec`` and return the parsed result dict.

    Extra ``params`` are appended as ``key=value``: strings pass through (synowebapi reads them as
    JSON strings), everything else (objects, bools) is ``json.dumps``'d so the CLI parses it as the
    right JSON type — this is how the profile object and ``profile_applying:true`` are passed.
    """
    argv = ["synowebapi", "--exec", f"api={api}", f"method={method}", f"version={version}"]
    for key, val in params.items():
        argv.append(f"{key}={val if isinstance(val, str) else json.dumps(val)}")
    return _parse_webapi(host.run(argv, timeout=timeout).get("stdout") or "")


def _info(host) -> dict:
    """``synofirewall --info`` -> parsed JSON (single line). DSM's own firewall collector."""
    r = host.run(["synofirewall", "--info"])
    body = (r.get("stdout") or "").strip()
    try:
        return json.loads(body) if body.startswith("{") else {"_parse_error": True, "raw": body[:400]}
    except json.JSONDecodeError:
        return {"_parse_error": True, "raw": body[:400]}


def _settings_file(host) -> dict:
    r = host.run(["cat", FW_SETTINGS_PATH])
    body = (r.get("stdout") or "").strip()
    if r.get("rc") != 0 or not body.startswith("{"):
        return {"_unreadable": True, "rc": r.get("rc")}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_parse_error": True, "raw": body[:400]}


def _enforced(host) -> tuple[bool, str]:
    """Runtime enforcement check from live iptables. Uses the whole-table dump (per-chain `-S
    INPUT` errors on old DSM kernels). Enforced == the firewall's own chain is loaded and INPUT
    jumps to it."""
    out = host.run(["iptables", "-S"]).get("stdout") or ""
    enforced = "INPUT_FIREWALL" in out and "-A INPUT -j INPUT_FIREWALL" in out
    return enforced, out


def _profile(host, name: str) -> dict:
    """webapi profile get -> ``data`` ({global:{policy,rules[]}, name}) or an error marker."""
    res = webapi(host, "SYNO.Core.Security.Firewall.Profile", "get", name=name)
    if res.get("success") and isinstance(res.get("data"), dict):
        return res["data"]
    return {"_error": res.get("error") or res, "name": name}


# ── pure rule logic (unit-tested; no host) ───────────────────────────────────────────────────────

def validate_rule(rule: dict) -> list[str]:
    """Return a list of human-readable problems with one rule (empty list == valid)."""
    errs: list[str] = []
    if not isinstance(rule, dict):
        return [f"rule is not an object: {rule!r}"]
    if rule.get("policy") not in POLICIES:
        errs.append(f"policy must be one of {sorted(POLICIES)} (got {rule.get('policy')!r})")
    pg = rule.get("port_group")
    if pg not in PORT_GROUPS:
        errs.append(f"port_group must be one of {sorted(PORT_GROUPS)} (got {pg!r})")
    if rule.get("protocol", "all") not in PROTOCOLS:
        errs.append(f"protocol must be one of {sorted(PROTOCOLS)} (got {rule.get('protocol')!r})")
    if rule.get("port_direction", "destination") not in PORT_DIRECTIONS:
        errs.append(f"port_direction must be one of {sorted(PORT_DIRECTIONS)}")
    sg = rule.get("source_ip_group")
    if sg not in SOURCE_GROUPS:
        errs.append(f"source_ip_group must be one of {sorted(SOURCE_GROUPS)} (got {sg!r})")
    src = rule.get("source_ip", "")
    if sg == "netmask":
        try:
            ipaddress.ip_network(src, strict=False)
        except ValueError:
            errs.append(f"source_ip {src!r} is not a CIDR (required for source_ip_group=netmask)")
    elif sg == "ip":
        try:
            ipaddress.ip_address(src)
        except ValueError:
            errs.append(f"source_ip {src!r} is not an IP (required for source_ip_group=ip)")
    elif sg == "iprange":
        if not _parse_range(src):
            errs.append(f"source_ip {src!r} is not an a~b / a-b range (source_ip_group=iprange)")
    elif sg == "all" and src not in ("", "all"):
        errs.append("source_ip_group=all requires source_ip 'all'")
    if pg != "all" and not str(rule.get("ports", "")).strip():
        errs.append(f"port_group={pg} requires a non-empty 'ports'")
    return errs


def _parse_range(spec: str):
    for sep in ("~", "-"):
        if sep in (spec or ""):
            a, b = spec.split(sep, 1)
            try:
                return ipaddress.ip_address(a.strip()), ipaddress.ip_address(b.strip())
            except ValueError:
                return None
    return None


def _source_matches(rule: dict, ip: str) -> bool:
    sg, src = rule.get("source_ip_group"), rule.get("source_ip", "")
    if sg == "all" or src == "all":
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if sg == "ip":
        try:
            return addr == ipaddress.ip_address(src)
        except ValueError:
            return False
    if sg == "netmask":
        try:
            return addr in ipaddress.ip_network(src, strict=False)
        except ValueError:
            return False
    if sg == "iprange":
        rng = _parse_range(src)
        return bool(rng) and rng[0] <= addr <= rng[1]
    # geoip: a private LAN management IP does not resolve to a country -> never matches. Treating
    # it as non-matching is the safe call for the lock-out guard (we never assume a geoip rule
    # would let the operator back in).
    return False


def _ports_cover(rule: dict, service_name: str, port: int, proto: str = "tcp") -> bool:
    if rule.get("protocol", "all") not in ("all", proto):
        return False
    pg, ports = rule.get("port_group"), str(rule.get("ports", ""))
    if pg == "all" or ports.strip() == "all":
        return True
    tokens = [p.strip() for p in ports.split(",") if p.strip()]
    if pg in ("reserved", "service", "system"):
        return service_name in tokens
    if pg in ("custom", "self-defined"):
        for tok in tokens:
            tok = tok.split("/")[0]  # strip optional /tcp,/udp suffix
            if ":" in tok or "-" in tok:
                lo, hi = (tok.replace("-", ":").split(":") + [""])[:2]
                try:
                    if int(lo) <= port <= int(hi):
                        return True
                except ValueError:
                    continue
            else:
                try:
                    if int(tok) == port:
                        return True
                except ValueError:
                    continue
    return False


def evaluate_access(rules: list[dict], ip: str, service_name: str, port: int,
                    proto: str = "tcp") -> str:
    """First-match evaluation over the ordered rule list -> 'allow' | 'drop' | 'implicit_deny'.

    No matching rule means the implicit default-deny tail an enabled DSM firewall applies — we
    treat unmatched as denied (the conservative assumption for a lock-out check)."""
    for r in rules:
        if not r.get("enable", True) or r.get("policy") not in POLICIES:
            continue
        if _ports_cover(r, service_name, port, proto) and _source_matches(r, ip):
            return r["policy"]
    return "implicit_deny"


def _sample_ip(management_source: str) -> str:
    """A concrete IP to probe the guard with, derived from the operator's management source."""
    ms = (management_source or "all").strip()
    if ms == "all":
        return "192.168.255.254"  # any private IP works since 'all' matches everything
    try:
        return str(ipaddress.ip_address(ms))
    except ValueError:
        pass
    try:
        net = ipaddress.ip_network(ms, strict=False)
        return str(next(net.hosts(), net.network_address))
    except ValueError:
        pass
    rng = _parse_range(ms)
    if rng:
        return str(rng[0])
    raise ValueError(
        f"management_source {management_source!r} must be 'all', an IP, a CIDR, or an a~b range "
        f"(a geoip/country source can't be verified for lock-out safety)"
    )


def lockout_guard(rules: list[dict], management_source: str = "all") -> dict:
    """Would these rules, if active+enforced, still let the operator reach SSH and DSM from their
    LAN? Returns {ok, sample_ip, checks{...}, reason, suggested_rule?}.

    This is the *human fallback* check — the relay/tailnet path is loopback and can never be cut."""
    try:
        sample = _sample_ip(management_source)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc), "checks": {}}

    ssh = evaluate_access(rules, sample, MGMT_SSH[0], MGMT_SSH[1])
    dsm_results = {f"{name}:{port}": evaluate_access(rules, sample, name, port)
                   for name, port in MGMT_DSM}
    ssh_ok = ssh == "allow"
    dsm_ok = any(v == "allow" for v in dsm_results.values())
    checks = {"ssh:22": ssh, **dsm_results, "from": sample}

    out = {"ok": ssh_ok and dsm_ok, "sample_ip": sample, "checks": checks}
    if not out["ok"]:
        missing = []
        if not ssh_ok:
            missing.append("SSH (22)")
        if not dsm_ok:
            missing.append("DSM (5000/5001)")
        out["reason"] = (
            f"would block {', '.join(missing)} from {sample} (management_source="
            f"{management_source!r}) once enforced — direct LAN admin access could be lost"
        )
        out["suggested_rule"] = {
            "enable": True, "name": "mgmt-allow", "policy": "allow",
            "port_direction": "destination", "port_group": "reserved",
            "ports": "ssh,dms,dms_https", "protocol": "tcp",
            "source_ip_group": "all" if management_source in ("all", "") else "netmask",
            "source_ip": management_source if management_source not in ("all", "") else "all",
            "_note": "prepend this (rules are first-match) to keep LAN admin access",
        }
    return out


def normalize_rule(rule: dict) -> dict:
    """Fill defaults and drop unknown keys so we send DSM exactly the fields it round-trips."""
    return {
        "enable": bool(rule.get("enable", True)),
        "name": str(rule.get("name", "")),
        "policy": rule["policy"],
        "port_direction": rule.get("port_direction", "destination"),
        "port_group": rule["port_group"],
        "ports": str(rule.get("ports", "all")),
        "protocol": rule.get("protocol", "all"),
        "source_ip_group": rule["source_ip_group"],
        "source_ip": str(rule.get("source_ip", "all")),
        "log": bool(rule.get("log", False)),
    }


# ── tool registration ─────────────────────────────────────────────────────────────────────────

def register_firewall_tools(mcp, host) -> None:
    """Register the DSM firewall Tier-A (audit/diagnose) and Tier-B (guarded mutation) tools."""

    def _status_core() -> dict:
        info = _info(host)
        wapi = webapi(host, "SYNO.Core.Security.Firewall", "get")
        wdata = wapi.get("data") if isinstance(wapi, dict) else None
        settings = _settings_file(host)
        enforced, _ipt = _enforced(host)

        # Three independent enable oracles; only call it authoritative when they agree.
        votes = {
            "synofirewall_info": (info.get("fw_enabled") == 1) if "fw_enabled" in info else None,
            "webapi": wdata.get("enable_firewall") if isinstance(wdata, dict) else None,
            "settings_file": settings.get("status") if "status" in settings else None,
        }
        present = [v for v in votes.values() if v is not None]
        if present and all(v == present[0] for v in present):
            enabled, confidence = present[0], "authoritative" if len(present) >= 2 else "heuristic"
        elif present:
            enabled, confidence = None, "conflict"
        else:
            enabled, confidence = None, "unknown"

        active_profile = (
            (wdata or {}).get("profile_name") if isinstance(wdata, dict) else None
        ) or settings.get("profile")

        return {
            "enabled": enabled,
            "confidence": confidence,
            "enforced": enforced,
            "active_profile": active_profile,
            "notify_enabled": bool(info.get("fw_notify_enabled")) if "fw_notify_enabled" in info else None,
            "num_profiles": info.get("num_profiles"),
            "drift": (
                None if enabled is None or enabled == enforced else
                "enabled-but-not-enforced (config says on, iptables has no INPUT_FIREWALL — reload?)"
                if enabled else
                "enforced-but-not-enabled (iptables has INPUT_FIREWALL but config says off)"
            ),
            "enable_oracles": votes,
            "note": "Tailscale is userspace here (no tailscale0); the relay/tailnet path rides "
                    "loopback, which the firewall always ACCEPTs — so the firewall governs the "
                    "physical LAN only and cannot cut this relay. Lock-out risk = direct LAN "
                    "SSH/DSM.",
        }

    @mcp.tool()
    def firewall_status() -> dict:
        """Tier A — authoritative DSM firewall posture: enabled? actually enforced? active profile?

        Enable state is corroborated across three oracles (synofirewall --info, the Firewall
        webapi, and firewall_settings.json) — reported 'authoritative' only when they agree,
        'conflict' when they don't, never silently collapsed. 'enforced' is read from live
        iptables, so config-vs-runtime drift (enabled but never reloaded) is surfaced separately.
        """
        return _status_core()

    @mcp.tool()
    def firewall_rules(
        profile: Annotated[str | None, Field(description="profile name; default = active profile")] = None,
    ) -> dict:
        """Tier A — enumerate a profile's rules in the clean webapi form (policy allow/drop,
        source_ip_group all/ip/netmask/iprange/geoip, literal source_ip), plus the iptables the
        firewall would generate (synofirewall --enum IPV4) and DSM's raw --info view. Defaults to
        the active profile. The webapi rule shape here is exactly what firewall_set_rules accepts.
        """
        name = profile or _status_core().get("active_profile") or "default"
        data = _profile(host, name)
        rules = (data.get("global") or {}).get("rules") if isinstance(data.get("global"), dict) else None
        return {
            "profile": name,
            "default_policy": (data.get("global") or {}).get("policy") if isinstance(data.get("global"), dict) else None,
            "rules": rules if rules is not None else data,
            "rule_count": len(rules) if isinstance(rules, list) else None,
            "generated_iptables": (host.run(["synofirewall", "--enum", "IPV4"]).get("stdout") or "").strip(),
            "profiles": _profile_list(host),
        }

    @mcp.tool()
    def firewall_diagnose(
        management_source: Annotated[
            str, Field(description="LAN source to test admin reachability from: 'all', an IP, a "
                                   "CIDR (e.g. 192.168.1.0/24), or an a~b range")
        ] = "all",
    ) -> dict:
        """Tier A — diagnose firewall issues: reconcile configured-vs-enforced state, check geoip
        readiness for any country rules, list adapters, and SIMULATE whether SSH/DSM admin access
        survives from `management_source` (first-match evaluation of the active profile). Flags the
        classic 'configured but not enforced' drift and the userspace-Tailscale caveat (the relay
        path is loopback and is never affected). Read-only.
        """
        status = _status_core()
        active = status.get("active_profile") or "default"
        data = _profile(host, active)
        rules = (data.get("global") or {}).get("rules")
        rules = rules if isinstance(rules, list) else []

        uses_geoip = any(r.get("source_ip_group") == "geoip" for r in rules)
        geoip_db = host.run(["sh", "-c", "ls -l /usr/share/xt_geoip 2>&1; ls /var/db/geoip-database 2>&1 | head"])
        geoip_mod = host.run(["sh", "-c", "lsmod 2>/dev/null | grep -i geoip || echo not-loaded"])

        findings: list[str] = []
        if status.get("drift"):
            findings.append(status["drift"])
        if status.get("confidence") == "conflict":
            findings.append(f"enable-state oracles disagree: {status['enable_oracles']}")
        guard = lockout_guard(rules, management_source) if rules else {
            "ok": None, "reason": "active profile has no readable rules"}
        if status.get("enabled") and rules and not guard.get("ok"):
            findings.append(
                f"ACTIVE+ENABLED firewall would not admit admin from {management_source!r}: "
                f"{guard.get('reason')}")
        if uses_geoip and "not-loaded" in (geoip_mod.get("stdout") or ""):
            findings.append("active profile has geoip rules but the geoip xt module is not loaded "
                            "(expected when the firewall is off; verify after enabling)")

        return {
            "status": status,
            "adapters": (host.run(["synofirewall", "--enum-adapter"]).get("stdout") or "").strip(),
            "management_access": guard,
            "uses_geoip": uses_geoip,
            "geoip": {"db": (geoip_db.get("stdout") or "").strip(), "module": (geoip_mod.get("stdout") or "").strip()},
            "findings": findings or ["no issues detected"],
            "note": "management_access is the human LAN fallback (SSH/DSM over the physical adapter). "
                    "This relay reaches the box via loopback/userspace-Tailscale and is unaffected "
                    "by firewall rules.",
        }

    @mcp.tool(annotations={"destructiveHint": True})
    def firewall_enable() -> dict:
        """Tier B — enable DSM firewall enforcement (synofirewall --enable), then verify.

        SAFETY: this relay is NOT at risk (its ingress is loopback/userspace-Tailscale, always
        accepted). The risk is DIRECT LAN access (SSH/DSM over the physical adapter): if the active
        profile lacks an allow rule for your LAN management subnet, enabling can lock out direct
        admin. Run firewall_diagnose(management_source=<your LAN CIDR>) FIRST — it refuses-by-
        analysis if SSH/DSM wouldn't survive. Enabling applies the current active profile.
        """
        before = _status_core()
        res = host.run(["synofirewall", "--enable"], timeout=60)
        after = _status_core()
        return {"action": "enable", "result": res, "before": _brief(before), "after": _brief(after)}

    @mcp.tool(annotations={"destructiveHint": True})
    def firewall_disable() -> dict:
        """Tier B — disable DSM firewall enforcement (synofirewall --disable), then verify. This
        OPENS access (removes the allow-list); it cannot lock you out."""
        before = _status_core()
        res = host.run(["synofirewall", "--disable"], timeout=60)
        after = _status_core()
        return {"action": "disable", "result": res, "before": _brief(before), "after": _brief(after)}

    @mcp.tool(annotations={"destructiveHint": True})
    def firewall_reload() -> dict:
        """Tier B — reapply the active profile to live iptables (synofirewall --reload), then
        verify. Use to clear 'enabled-but-not-enforced' drift after a config change."""
        before = _status_core()
        res = host.run(["synofirewall", "--reload"], timeout=60)
        after = _status_core()
        return {"action": "reload", "result": res, "before": _brief(before), "after": _brief(after)}

    @mcp.tool(annotations={"destructiveHint": True})
    def firewall_set_rules(
        rules: Annotated[
            list, Field(description="ordered allow-list (first-match) in the webapi rule form: each "
                        "{policy:allow|drop, port_group:all|reserved|service|custom|system|"
                        "self-defined, ports:csv-or-'all', protocol:all|tcp|udp, source_ip_group:"
                        "all|ip|netmask|iprange|geoip, source_ip, name?, enable?}. Same shape "
                        "firewall_rules returns.")],
        profile: Annotated[str | None, Field(description="profile to write; default = active")] = None,
        management_source: Annotated[
            str, Field(description="LAN source whose SSH/DSM access the guard protects: 'all', an "
                       "IP, a CIDR, or an a~b range")] = "all",
        default_policy: Annotated[
            str | None, Field(description="optional global default policy override (advanced); "
                              "left as-is when None")] = None,
        override_lockout_guard: Annotated[
            bool, Field(description="DANGEROUS: apply even if the guard says admin access would be "
                        "lost")] = False,
    ) -> dict:
        """Tier B — replace a profile's global rule list (guarded). Edits via the DSM webapi
        (Profile set + Apply), so literal IPs/subnets are encoded by DSM, not hand-built.

        A lock-out guard simulates the resulting rules and REFUSES (unless override_lockout_guard)
        any change that would deny SSH or DSM admin from `management_source` once enforced — the
        ALLOWED_USERS-style "don't strand yourself" rule. The relay's own loopback/tailnet path is
        never at risk. To round-trip: firewall_rules(profile) -> edit the 'rules' list -> pass here.
        """
        # Validate ALL inputs before any host work, so a bad request refuses with zero host calls.
        if not isinstance(rules, list) or not rules:
            return {"refused": True, "reason": "rules must be a non-empty list"}
        problems = {i: errs for i, r in enumerate(rules) if (errs := validate_rule(r))}
        if problems:
            return {"refused": True, "reason": "invalid rule(s)", "problems": problems}
        if default_policy is not None and default_policy not in POLICIES:
            return {"refused": True,
                    "reason": f"default_policy must be one of {sorted(POLICIES)} "
                              f"(got {default_policy!r})"}

        status = _status_core()
        name = profile or status.get("active_profile") or "default"

        norm = [normalize_rule(r) for r in rules]
        guard = lockout_guard(norm, management_source)
        if not guard.get("ok") and not override_lockout_guard:
            return {"refused": True, "reason": "lock-out guard: " + guard.get("reason", "admin "
                    "access not preserved"), "guard": guard,
                    "hint": "prepend guard.suggested_rule, widen management_source, or pass "
                            "override_lockout_guard=True if you truly intend this"}

        current = _profile(host, name)
        if current.get("_error"):
            return {"refused": True, "reason": f"cannot read profile {name!r}", "detail": current}
        glob = dict(current.get("global") or {})
        if default_policy is not None:
            glob["policy"] = default_policy
        glob["rules"] = norm
        new_profile = {**current, "global": glob, "name": name}

        # Persist with profile_applying:false — this writes the rules DIRECTLY to the named profile
        # (works for any profile, firewall on or off, no staging file). We deliberately do NOT use
        # profile_applying:true: that is a two-phase commit that writes a `.test_<name>` staging
        # profile and needs a follow-up Profile.Apply to promote it — and a failed Apply (e.g.
        # error 120 on a non-active profile) both fails to persist AND orphans the staging file.
        set_res = webapi(host, "SYNO.Core.Security.Firewall.Profile", "set", timeout=60,
                         profile=new_profile, profile_applying=False)
        if not set_res.get("success"):
            return {"applied": False, "reason": "Profile set failed (rules NOT changed)",
                    "detail": set_res, "guard": guard}

        # Push to live iptables only when this IS the active profile AND the firewall is enabled —
        # `synofirewall --reload` regenerates the live ruleset from the active profile's stored
        # config. Otherwise the saved rules are dormant until the profile is activated + enabled.
        is_active = name == status.get("active_profile")
        live = is_active and bool(status.get("enabled"))
        reload_res = host.run(["synofirewall", "--reload"], timeout=60) if live else None

        return {
            "applied": True,
            "profile": name,
            "live": live,
            "live_note": ("reloaded into live iptables" if live else
                          f"saved to profile {name!r} but dormant — active={status.get('active_profile')!r}, "
                          f"enabled={status.get('enabled')} (becomes live when this profile is active "
                          f"and the firewall is enabled)"),
            "guard": guard,
            "guard_overridden": (not guard.get("ok")) and override_lockout_guard,
            "set": set_res.get("success"),
            "reload": (reload_res.get("rc") == 0) if reload_res else None,
            "rules_written": len(norm),
            "status_after": _brief(_status_core()),
            "rules_after": (_profile(host, name).get("global") or {}).get("rules"),
        }


def _profile_list(host) -> list[str]:
    out = host.run(["synofirewall", "--profile-list"]).get("stdout") or ""
    return [ln.strip() for ln in out.splitlines() if ln.strip() and ":" not in ln and ln.strip() != "Profile names:"]


def _brief(status: dict) -> dict:
    return {k: status.get(k) for k in ("enabled", "confidence", "enforced", "active_profile", "drift")}
