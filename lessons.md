# Lessons

Engineering lessons from building `mage-hands` and deploying `synology-hands` to a live
Synology NAS — the design calls that held up, and the surprises found between "it compiles" and
"it runs on the box."

## The threat isn't the intruder, it's the over-eager agent

For an ephemeral, tailnet-only, token-gated relay, the external attack surface is small. The
realistic failure mode is **accidental destructive execution**: Claude does exactly what was
asked, but the intent was underspecified. So the dangerous tool isn't gated by confirmation
alone — `run()` is a two-call state machine (dry-run returns a one-time `exec_token` bound to
the exact command; execution replays it), and a regex denylist refuses catastrophic patterns
*before* a token is ever issued.

**Lesson:** when a single tool call can be irreversible, make the danger require *friction
that survives a hallucinated follow-up* — a replayed token tied to the exact command, not a
boolean the model can just set to `true`.

## A "harmless" read tool is the real exfiltration vector

`run()` is obviously dangerous and gets all the gating attention. `read_file()` looks benign —
and is therefore the tool most likely to quietly read `/etc/shadow`, ssh keys, or Tailscale
state when an agent decides to "inspect this config to help debug." It gets a strict allow/deny
**path policy**, not just a traversal guard.

**Lesson:** rank tools by *what an over-helpful caller would do with them*, not by how dangerous
they look. The innocuous reader needs a policy as much as the scary executor.

## Security here is isolation + ephemerality, not sandboxing — say so

The relay is `privileged` + `pid:host` + `/:/host`; once up it is root on the NAS. Pretending
otherwise would make the whole design incoherent. The safety properties are explicit and
elsewhere: tailnet-only ingress, a per-box token, the relay usually *not existing*, and a
forensic audit log. `restart: "no"` plus an idle watchdog are load-bearing, not conveniences.

**Lesson:** if you can't sandbox, don't fake it. Name the trust boundary honestly and put your
controls where they actually are — the strongest safety property here is "the relay usually
isn't running."

## Verify the auth path on the real handshake, not a bare GET

A `curl /mcp` returning 200 proves nothing about auth — the streamable-HTTP endpoint answers
negotiation before tool dispatch. The smoke test does a real MCP `initialize` + `tools/list`
with a good token (must list tools) and a bad token (must 401). The bad token came back
`HTTPStatusError` (401); a naive GET check would have "passed" while auth was wrong.

**Lesson:** test the auth boundary with the actual protocol flow a client uses, including the
negative case. "It returned something" is not "it enforced the rule."

## `get_http_headers()` strips `authorization` by default

The first auth design read the bearer header inside middleware via
`get_http_headers()` — which **silently omits `authorization`/`host`** unless you pass
`include={...}`. It would have seen no token and behaved unpredictably. The fix was to stop
hand-rolling auth entirely and use fastmcp's built-in `StaticTokenVerifier` (`auth=`), which
returns a spec 401 at the transport layer before any tool runs; `get_http_headers` is then used
only for the *non-stripped* `tailscale-user-*` headers (still requested via `include`).

**Lesson:** prefer the framework's auth primitive over reading headers by hand — and read the
defaults of any "give me the request headers" helper, because the security-relevant ones are
exactly the ones that tend to be filtered.

## Probe for moving import paths instead of pinning a guess

`StaticTokenVerifier` has lived at different module paths across fastmcp builds. Rather than
hard-code one and crash-loop on a mismatch, `auth.py` tries the known locations in order and
raises a clear error naming all of them if none resolve. On the live box it imported on the
first deploy — but the probe made that a non-event instead of a gamble.

**Lesson:** for a fast-moving dependency, a small import probe with a loud, specific failure
beats a single pinned path you hope is current.

## Let the test rewrite your expectations — the trailing-slash gap

A unit check of the denylist flagged `rm -rf /volume1/docker/app/cache` as "not blocked" —
which turned out to be *correct* (targeted deletes under a volume are legitimate work). But the
same run revealed a genuine hole: `rm -rf /volume1/`, `/volume1/*`, and `/*` all slipped past
because the patterns only matched the bare path. Those *do* wipe the pool/root. Tightened the
regexes to cover trailing-slash and glob forms while still allowing deep targeted deletes.

**Lesson:** when a test "fails," first decide whether the test or the code is wrong — then keep
going, because the same fixture often exposes a real adjacent bug you weren't looking for.

## Deploy the allowlist empty, then tighten from the audit log

`ALLOWED_USERS` enforces the Tailscale caller identity. Guessing it wrong locks you out
completely. So the relay went up with it **empty** (token + ACL only), one call confirmed the
real identity in `audit.jsonl` (a Tailscale login that was notably *not* the git email), and
only then was the allowlist set and the container recreated.

**Lesson:** for a control that can lock you out, observe the real value in production before
enforcing it. The audit log you built for forensics is also your safe configuration oracle.

## Synology fights you in small, specific ways

Five concrete gotchas, each of which silently broke a step until found:
- **Key auth needs tight home perms.** `ssh-copy-id` added the key but login still failed until
  `~` and `~/.ssh` were `700` (it doesn't fix the home dir itself).
- **`sudo` `secure_path` excludes `/usr/local/bin`** and the Tailscale package dir, so
  `sudo docker` / `sudo tailscale` were "command not found." Scripts resolve full paths.
- **Bind-mount sources must pre-exist** — the daemon refused to start the container until
  `./logs` existed (no auto-create).
- **`/etc/crontab` is regenerated by DSM Task Scheduler** — hand-edits get clobbered, so the
  idle watchdog goes in via the Task Scheduler GUI, not crontab.
- **Container Manager's GUI can't set `privileged`** — the host-admin stack must be deployed
  via SSH `docker compose`.

**Lesson:** an appliance OS is not a generic Linux box. Budget a recon pass before deploy and
encode each quirk (full binary paths, pre-created mounts, GUI-vs-CLI) into the scripts so the
next box just works.

## `nsenter -t 1` beats mounting the docker socket

Driving the host through `nsenter` into PID 1's namespaces means the relay uses the host's own
`docker`, `smartctl`, and `syno*` binaries — sidestepping Synology's non-standard docker-socket
path entirely and avoiding a second root-equivalent surface. A bonus property: container env
vars (including `RELAY_TOKEN`) don't propagate into host execution, so the token can't leak into
host process listings.

**Lesson:** when a privileged container must administer its host, entering the host namespaces is
often cleaner and less leaky than mounting daemon sockets — and it inherits the host's tooling
for free.

## One token transfer, then key auth — keep the password off disk only as long as needed

Bootstrapping used the admin password exactly once (via `expect`) to install an SSH key; sudo
then ran via `sudo -S` fed from a 0600 file. The moment the deploy finished, that file was
`shred`'d. The relay's own bearer token never touches the NAS shell history — it's written into
`.env` base64-wrapped in transit.

**Lesson:** treat a shared human password as a bootstrap-only credential with a deletion plan,
and keep service secrets out of command lines and shell history from the start.

## Scoped NOPASSWD is a property of the whole path, not the sudoers line

Granting the relay user passwordless sudo for the lifecycle scripts is what lets Claude start the
server unattended. But a NOPASSWD'd script the user can *edit* is just passwordless arbitrary
root — they'd rewrite it. So the copies live at `/usr/local/sbin/mage-hands-relay-{up,down}`,
root-owned, with a root-owned parent so the user can't even directory-swap them. The first
instinct — a `.bin` subdir inside the (relay-user-owned) deploy tree — would have reopened the
hole: deleting/replacing a file depends on write permission of its *parent directory*, not the
file's own ownership. And the install failed loudly first because `/usr/local/sbin` didn't exist
on the box (only `/usr/local/bin`), a reminder to not assume standard dirs on an appliance OS.
Verify the scope the boring way: `sudo -n <lifecycle-script>` must succeed and `sudo -n id` must
fail with "a password is required."

**Lesson:** "scoped NOPASSWD" only holds if the granted command *and every directory above it*
are unwritable by the granted user. Audit the path, not just the sudoers entry — and prove the
negative (general sudo still prompts), not just the positive.

## Entering the host namespace gives you its binaries, not its PATH

Updating Tailscale through the relay (`tailscale update --yes` via `run()`) downloaded and
signature-verified the new SPK, then died: `synopkg install failed: exit status 127 — synopkg: No
such file or directory`. `tailscale update` shells out to `synopkg` by bare name, but the relay
runs commands via `nsenter -t 1` into PID 1's namespaces with a bare PATH that omits
`/usr/syno/bin` — and the same bites cron / Task-Scheduler jobs. Re-running with an explicit
`PATH=/usr/syno/bin:/usr/syno/sbin:…` let synopkg resolve and the install finished
(1.58.2 → 1.98.2). The reason we were updating by hand at all: Synology Package Center never
surfaced the update despite the box being ~2 years behind — the working path is Tailscale's own
`tailscale update --yes`, not Package Center.

**Lesson:** `nsenter` into the host gives you its *binaries* but not its login *PATH*. Any tool
that itself calls DSM utilities (`synopkg`, `synoservicectl`, …) by bare name needs the syno bin
dirs put back on PATH — and check the exit code, since the 127 hid behind otherwise-healthy
download output.

## When the host is slow, suspect the host daemon, not your container

kappa's CPU "stayed high" after we started using it, and the easy story was "the relay is heavy."
It wasn't — the relay was a near-idle uvicorn process and was in fact already stopped by the idle
watchdog. The actual hog was `tailscaled` (the old 1.58.2) stuck at **364%**. The tell: measure,
don't assume — `top` plus a per-PID `/proc/<pid>/stat` delta named the culprit in seconds, and the
fix was a daemon restart (immediate) + version update (durable), nothing to do with mage-hands.

**Lesson:** a new component is a tempting scapegoat for a pre-existing/adjacent problem. Attribute
load to a measured PID before redesigning the thing you just shipped.

## A new target type is a Runner, not a fork

Adding the ASUS Merlin router — a box with no Docker, no nsenter, and a BusyBox userland — turned
out to need *zero* changes to the gating, audit, read-policy, or tool-dispatch code. It was one new
`Runner` (`SSHRunner`) plus a `runner_reader` for reads-over-the-Runner. Everything above the
transport seam (`run()`'s dry-run/token gate, `DEFAULT_DENY`, `PathPolicy`, the audit middleware)
was already transport-agnostic because it only ever calls `runner.run([...])`. The router itself
stays stock: SSH on + one public key. Two real gotchas surfaced at the transport, though:
dropbear gives non-interactive sessions a near-empty environment and ignores `AcceptEnv`, so bare
tool names (`wl`, `nvram`, `iptables`) die with exit 127 until you prepend an explicit `PATH`; and
`shlex.join` (not token-passing after `--`) is the load-bearing choice that makes `["sh","-c",cmd]`
round-trip with exactly one remote shell evaluation.

**Lesson:** if your dangerous-operation gating sits above a clean execution seam, a wholly
different *kind* of target is an additive Runner, not a new codebase. But verify the remote shell's
environment assumptions — a stripped PATH and quoting are where "it works locally" breaks.

## Give the appliance its own identity with a sidecar, not a borrowed port

router-hands runs on kappa, whose `:443` is already serving synology-hands. Rather than multiplex
paths on kappa's node, the relay shares a network namespace with a `tailscale/tailscale` **sidecar**
(`network_mode: service:tailscale`) that joins the tailnet as its own node `router1` and serves
declaratively (`TS_SERVE_CONFIG`). Clean MagicDNS, no privileged container, no host-port juggling.
Two edges to know: the relay must bind `127.0.0.1` *inside the shared netns* (so the smoke test runs
from inside the container, not kappa's host loopback), and in userspace mode (`TS_USERSPACE=true`)
only tailnet traffic uses the netstack — LAN egress to the router rides the Docker bridge, so
`relay-up.sh` verifies SSH reachability explicitly (fall back to kernel-TUN if it fails).

**Lesson:** when a second appliance lands on a host that already owns `:443`, give it its own tailnet
identity with a sidecar instead of contorting the existing node — but remember that "share the
sidecar's namespace" changes where loopback lives and how non-tailnet egress is routed.

## "Disabled," or "we asked the wrong oracle"? An empty probe is not a negative

A resilience audit cleared QuickConnect on both NAS as "not configured." It was **enabled the whole
time** — relaying DSM and **SSH** to the public internet via `*.quickconnect.to`, while SSH still
allowed password auth. The audit had probed `/etc/synoinfo.conf` (which has no `quickconnect` key)
and `synogetkeyvalue` against `/usr/syno/etc/synoinfo*.conf` files that **don't exist on DSM 7** —
and `synogetkeyvalue` on a missing file returns **rc 0 + empty**. Empty was read as "off." The
authoritative source turned out to be `/usr/syno/etc/synorelayd/synorelayd.conf`
(`"quickconnect":{"enabled":true}` + the relayed service list), corroborated by the running
`synorelayd` daemon and `synowebapi … SYNO.Core.QuickConnect get`. The same wrong-file class also hid
the auto-block state (the real source is the `SYNO.Core.Security.AutoBlock` webapi). The structural
fix is the `internet_exposure` tool: every channel returns `{enabled, source, confidence}` where
confidence is `authoritative | heuristic | unknown`, **`unknown` is never collapsed into
`disabled`**, and a config value is confirmed against an independent runtime signal (is the daemon
actually running?) before any security-relevant negative.

**Lesson:** a probe that returns nothing has two causes — the feature is off, or you queried the
wrong oracle — and a security tool must never conflate them. Carry provenance and a confidence level,
make "unknown" a first-class state distinct from "disabled," and corroborate config with a runtime
signal. Absence of evidence is not evidence of absence.

## `nsenter` gives you the host's binaries, but DSM moved them (synoservicectl → synosystemctl)

The PATH fix made `syno*` tools resolve — and immediately surfaced that `service_status` /
`restart_service` had been calling **`synoservicectl`, which doesn't exist on DSM 7** (it returns
127). DSM 7 replaced it with `synosystemctl` (`get-active-status` / `reload-or-restart`). The bug
was invisible before only because the *old* relay had no `/usr/syno` PATH, so the same tools failed
with the same 127 for a *different* reason — two faults masking each other.

**Lesson:** when you fix the reason a class of commands silently fails, re-test everything that
depended on them — a PATH fix can unmask a stale binary name. Appliance OSes rename their own
tooling across majors; pin the verb to the OS version, not to muscle memory.
