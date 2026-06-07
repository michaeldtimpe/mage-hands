# Contributing to mage-hands

Thanks for your interest. `mage-hands` runs **ephemeral, privileged relays** that give Claude
root on home-lab appliances, so contributions are held to a security-first bar. Please read this
whole file before opening a PR — most of it is about *not* weakening the safety model.

## Start here

- **[README.md](README.md)** — what the project is and how a relay works.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — request lifecycle, security model, tool tiers, audit schema, config.
- **[AGENTS.md](AGENTS.md)** — the practical guide for changing code: *Adding a tool*, *Adding an appliance*, *Tuning the denylist*, *Tuning the read policy*. Most PRs map to one of those tasks.
- **[CLAUDE.md](CLAUDE.md)** — working conventions (also the source of the rules below).

## Ground rules (non-negotiable)

These protect the security properties the whole project rests on. A PR that violates one will be
asked to change before anything else is reviewed.

1. **Secrets never get committed.** `.env`, `*.token`, `logs/`, `secrets/`, and `ts-state/` are
   gitignored — keep them that way. Use the `.env.example` files as the template; never put a real
   token, key, tailnet name, or host identity in tracked files or commit messages.
2. **The security model is isolation + auth + ephemerality + audit — not sandboxing.** A running
   relay is root on its host *by design*. Do **not** add capabilities that assume containment, and
   don't relax the loopback bind: the relay listens on `127.0.0.1` only and `tailscale serve` is
   the sole ingress. Never bind a routable interface; never use `tailscale funnel` (that's public).
3. **Cross-cutting logic lives in `common/` (`mage_hands_core`), not in an appliance.** Auth, audit,
   the gated `run()`, and the read-path policy are inherited. An appliance should only add a
   **Runner** + **tools**. If you find yourself reimplementing security logic in an appliance, it
   belongs in core.
4. **Keep raw execution behind the single gated `run()`.** Don't add ad-hoc shell-exec tools that
   bypass the dry-run → one-time-`exec_token` replay gate.
5. **Denylist and read policy: append, don't replace, and add tests.** `DEFAULT_DENY`
   (`common/mage_hands_core/exec.py`) and the `PathPolicy` deny list are safety backstops. If you
   change them, add unit tests covering the new patterns (see `common/tests/`), and prefer composing
   via `RUN_DENY_EXTRA` / `READ_*_EXTRA` over editing the defaults.

## Development setup

Authored on macOS (Apple Silicon) with **`uv`-first** Python tooling; Linux works too. You need
[`uv`](https://docs.astral.sh/uv/) and Python 3.12. No global package installs are required —
`uv run --with ...` pulls test deps into an ephemeral environment.

You do **not** need a real appliance to develop or test the core or the pure appliance logic — the
test suites use fakes/canned hosts and run fully offline.

## Running tests

Run each package's suite from its own directory:

```sh
# Core framework (common/)
cd common        && uv run --with pytest --with fastmcp pytest tests -q

# Synology appliance (pure firewall/lock-out logic, no live host)
cd synology-hands && uv run --with pytest python -m pytest tests -q

# Router appliance (offline parsers + import-time secret guard, FakeHost)
cd router-hands  && uv run --with pytest --with fastmcp --with ../common pytest tests -q
```

Please run the suites relevant to your change and add tests for new behavior — especially anything
touching the denylist, read policy, the `run()` gate, the audit log, or the identity allowlist.

## Submitting changes

1. Branch off `main`.
2. Make the change; keep it focused. Match the surrounding code's style and the existing tool-tier
   conventions in AGENTS.md (Tier A = read-only, Tier B = typed mutation with `destructiveHint`,
   Tier C = the gated `run()`).
3. Update docs that your change affects — the README repo-layout tree, ARCHITECTURE.md, and
   AGENTS.md are expected to stay in sync with the code.
4. Run the relevant test suites (above); add tests for new behavior.
5. Open a PR with a clear description of *what* changed and *why*, and call out explicitly if it
   touches any security-relevant path (auth, audit, denylist, read policy, `run()`, network bind).

## Reporting a security vulnerability

**Do not open a public issue for a security vulnerability.** Use GitHub's private reporting:
the repo's **Security** tab → **Report a vulnerability** (private security advisory). Include
affected component, reproduction, and impact. Please give a reasonable window to respond before any
public disclosure.
