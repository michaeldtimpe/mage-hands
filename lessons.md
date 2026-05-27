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

## Hardening a shared service can cascade — whitelist before you demand a password

alpha's Transmission RPC was wide open (`rpc-authentication-required: false`, bound `0.0.0.0:9091`).
The reflexive fix — turn on RPC auth with a username/password — would have **silently broken the
download pipeline**: Sonarr/Radarr/etc. are *clients* of that same RPC, so every `*arr` would have
lost its download client until each was re-configured with the new credentials (and a password the
operator never chose). Once QuickConnect was off the service was already LAN-only, so the
proportionate move was an **IP whitelist** (`127.0.0.1,192.168.1.*`) — closes the same door for the
threat that remains (a rogue LAN host is still possible, but not an internet one) without touching a
single integration. Auth stays available as a deliberate, later opt-in *with* the client-update plan.
(Also: Transmission rewrites `settings.json` on shutdown, so edit it **stopped**, not running.)

**Lesson:** before hardening a service, ask *who else authenticates to it.* A shared back-end's
"add auth" is a fan-out change, not a local one. Match the control to the exposure that actually
remains after the upstream fix, and reach for a whitelist (no shared secret, no cascade) before a
credential that every client must now learn.

## A management-API "disabled" can be a hardware fault three layers down

"Alpha's UPS health is broken." DSM's UPS webapi said `enable:false, status:usb_ups_status_unknown`
— which *reads* like "someone turned UPS off." But the persisted config (`synoups.conf`,
`ups_enabled="yes"`) disagreed, and a CyberPower UPS was physically cabled. Drilling down through the
layers: DSM (`ups-usb.sh`) auto-probes drivers and writes `tripplite_usb` only as the *give-up*
fallback after every driver returns an empty product → the log loop `This UPS is not supported.
product=[]` → `Stop UPS Daemon`. Running `usbhid-ups -DD` directly (the right driver for CyberPower)
got further — it *saw* `0764:0501` — but died on `could not claim interface 0: No such file or
directory`. The bottom of the stack: `/sys/.../2-3` showed the device enumerated with **zero
interfaces** (`0IFs`), and a software USB reset (unbind/rebind) didn't bring the interface back. So
the real fault is **physical** — a flaky USB cable/port (or a failing UPS USB controller) that lets
the device enumerate but never expose its HID interface. No driver, DSM's or NUT's, can claim an
interface that isn't there; the fix is a re-seat / cable swap / different port / power-cycle.

**Lesson:** a control-plane status (`enabled:false`, `unknown`, "not supported") is an *assertion by
the management layer*, not a root cause. When it contradicts the persisted config or the physical
reality, keep descending — service → daemon log → raw driver → `/sys` USB topology — until you hit a
layer that can't lie. Some "fix it in software" requests bottom out at a cable, and saying so plainly
(with the evidence) is the fix.

## The generic health tool is the wrong oracle for an appliance's cache

Asked to check alpha's SSD cache wear, the reflex is `smartctl -d nvme /dev/nvc1` — which fails
with *"Inappropriate ioctl for device,"* and there's no `nvme` or `synonvme` CLI to fall back to.
The reason is two layers of appliance-specific remapping: DSM renames cache SSDs to `nvc1`/`nvc2`,
and on the M2D17 card these are **M.2 SATA** drives (Intel D3-S4510) presented as **SCSI**, so the
NVMe admin path the tool name assumes simply doesn't exist. The actual wear data was sitting
pre-parsed in `/run/synostorage/disks/nvc{1,2}/` the whole time — `remain_life` (the % Storage
Manager shows) plus a `smart_info_list.cache` JSON of every SMART attribute. DSM had already polled
the drives; the job was to *read its answer*, not re-derive one with a tool that guesses the wrong
transport. (Same shape as the QuickConnect "wrong file" and the `synoservicectl→synosystemctl`
rename: the device is `nvc*` for "NVMe cache" by naming convention, but it's SATA underneath.)

**Lesson:** on an appliance, prefer the vendor's own cached/parsed state over a generic tool that
assumes a standard transport — the box has usually already done the read, and the standard tool's
*name* (`-d nvme`) can be a lie about what's physically there. When a health probe errors, ask
whether you reached for the wrong oracle before concluding the data is unavailable.

## The vendor webapi is the right oracle for *writes* too — and `profile_applying:true` is a trap

Building the DSM firewall tools, the read side was the familiar wrong-oracle dance: the
enable state is **not** in `/etc/synoinfo.conf` (`synogetkeyvalue` there returns rc 0 + empty —
the same false-negative that hid QuickConnect), it's in `synofirewall --info` (`fw_enabled`),
the `SYNO.Core.Security.Firewall get` webapi, and `firewall.d/firewall_settings.json` — so the
tool reads all three and only calls it "authoritative" when they agree. (Also: `iptables -S INPUT`
errors *"No chain by that name"* on kappa's 4.4 kernel while `iptables -S` works — parse the whole
table, never a single chain.) But the sharper lesson was on the **write** side. DSM stores rules
two ways: the profile JSON uses opaque integers (`policy:0`, `ipGroup:1`, `ipType:0`,
`ipList:["192.168.1.0","24"]` for a `/24`), while the webapi speaks clean strings
(`policy:"allow"`, `source_ip_group:"netmask"`, `source_ip:"192.168.1.0/24"`). Hand-encoding the
integer form would have been a guess-the-codes minefield; writing through `Profile set` lets DSM
encode it. The trap surfaced in a **reversible experiment on the non-active profile with the
firewall off**: `Profile set` with `profile_applying:true` returned `success:true` but the rules
**did not persist** — it's a two-phase commit that writes a `.test_<name>` *staging* profile and
needs a follow-up `Profile.Apply` to promote it; the Apply 120'd (non-active profile), so nothing
committed *and* it orphaned the staging profile (`num_profiles` silently went 2→3). The correct
primitive is `profile_applying:false` (persists directly, no staging) plus `synofirewall --reload`
to push live only when editing the active profile.

**Lesson:** the "ask the vendor's own tool" rule extends past reads — when a config has a clean
API representation and an opaque on-disk one, mutate through the API so the box does the encoding.
And prove a write *persisted* by reading it back, not by trusting a `success:true`: a two-phase
"apply" flow can report success on the staging write while the durable state is unchanged (and
leaves litter). Test mutations reversibly on an inactive/duplicate object first — it's how you find
the staging-orphan before it's your production profile.

## Userspace Tailscale means the firewall can't lock out the *relay* — so guard the human

The scary part of a firewall `set_rules` tool is stranding your own access (the `ALLOWED_USERS`
"deploy empty, then tighten" fear, but worse — a default-deny mistake locks you out at the network
layer). Reasoning about *who* could be stranded changed the whole guard design. These boxes run
Tailscale in **userspace** mode: there is no `tailscale0` interface; ingress is
`tailscale serve` → loopback, and DSM's generated `INPUT_FIREWALL` chain **always** begins
`-i lo -j ACCEPT` + `ESTABLISHED,RELATED -j ACCEPT`. So the relay's MCP path — and any
tailnet-sourced admin, which also lands on loopback — can **never** be cut by the firewall; it only
governs the physical LAN adapter (`ovs_bond0`). The real lock-out risk is a *human's direct LAN
SSH/DSM*, the fallback you'd want if the relay were down. So the guard doesn't try to protect the
bot (it's structurally safe); it simulates first-match rule evaluation for SSH(22)/DSM(5000/5001)
from the operator's declared LAN source and refuses any rule set that would deny them.

**Lesson:** before building a "don't strand yourself" guard, map the actual ingress paths and ask
which of them the control can even reach. Here the agent's own path was immune (loopback) and the
human's was not — so the guard protects the human. A safety check aimed at the wrong victim is
just friction; aim it at the access path that the change can actually sever.

## `nvram get <missing>` returns rc 0 + empty — the router-hands twin of the QuickConnect miss

On Asuswrt-Merlin, `nvram get <key-that-does-not-exist>` (and an unset/stripped key) exits **0 with
an empty string** — byte-for-byte the same trap that false-cleared QuickConnect on the NAS (a probe
that "succeeded" but was semantically empty, read as "feature off"). When `router-hands` grew an
`internet_exposure` tool whose whole job is to *not* report a wide-open box as closed, every channel
had to map **empty → `unknown`/`null`, never `disabled`** (and SSH's `sshd_enable=1↔2` LAN-vs-WAN
meaning has flipped across firmwares, so a nonzero value is `scope: "unknown (verify)"`, not "lan" —
a false-negative on WAN SSH is worse than a false-positive).

**Lesson:** "successful exit + empty output" is *absence of evidence*, not evidence of absence —
treat it as `unknown`, the same way across appliances. Other Merlin gotchas from the same build:
BusyBox `ps` has no `-eo/--sort` (use `top -bn1`, keep the **raw** lines — column order varies by
build; trust the two-sample `/proc/stat` delta for CPU, not top's header); CPU temp lives in
`/proc/dmu/temperature` and per-radio `wl -i <if> phy_tempsense` (which errors if the radio is
down), **not** `/sys/class/thermal`; never `nvram show` in a tool (it dumps `http_passwd`/
`*_wpa_psk`/`ddns_passwd`/VPN keys) — read a fixed safe-key allowlist and assert at import that no
allowlisted key looks secret.

## A reboot bypass the *default-on* flip activates: lexical denylists miss `service reboot`

`DEFAULT_DENY` anchors `reboot|shutdown|...` to *command position* (`_CMD`), which is right for the
NAS but leaves Merlin-valid indirect triggers wide open — verified empirically that `service reboot`,
`init 6`/`telinit 6`, `busybox reboot`, `rc reboot`, and `killall rc` all **pass** the core denylist.
Harmless while router `run()` was opt-in; the moment we flipped it **on by default** (for
synology-parity) those became live, ungated reboot paths. We added them to `ROUTER_DENY_EXTRA` so the
approval+`confirm`-gated `reboot_router` stays the only *intended* path — while documenting that
`sh -c reboot`/`echo reboot|sh` remain evadable (the denylist is a lexical backstop, not containment).

**Lesson:** turning a gated capability on by default isn't just a config change — it re-scopes the
threat model. Re-audit the backstops *against the target's own command vocabulary* (a multiplexer
like `service <verb>` or an alternate runlevel like `init 6` defeats a command-position regex), and
keep the prose honest: "the only directly-intended path," not "the only possible path." Also: a tool
that severs its own transport (`reboot` over SSH) must treat `transport_error`/rc 255 — and the
uncaught `subprocess.TimeoutExpired` from the executor — as *expected success*, not failure.

## A bare `sh -c` over SSH hits Broadcom's `sh` memory-tool, not the shell

Verified live on an RT-AX88U Pro (Merlin 3.0.0.6_102.7): every tool that goes through a `sh -c`
payload — `run()`, `internet_exposure`/`pending_updates` (via `_nvram_many`), and `performance`'s
`iowait`/`top_processes`/cpu fallback — returned `stderr: "sh: invalid option -- 'c'"` plus a
memory-tool usage banner (`dw/dh/db`, `sw/sh/sb`, `fw/fh/fb` = display/store/fill word/halfword/
byte). Cause: `SSHRunner` prepends `_MERLIN_PATH=/usr/sbin:/usr/bin:/sbin:/bin:...` and then
invokes a **bare** `sh -c`; Broadcom firmware ships a memory-diagnostic multicall binary whose
applet is literally named `sh` ("store halfword") in an sbin dir that precedes `/bin`, so `sh`
resolves to *it*, not busybox. Direct-argv tools (`system_info`, `wan_status`, `firewall_show`,
`clients`) were unaffected — they never invoke `sh` — which is exactly why the relay *looked*
healthy while `internet_exposure` silently reported every WAN channel as `unknown`/`null`.

**Lesson:** on Broadcom/ASUS targets, always invoke the shell by **absolute path** (`/bin/sh`),
never a bare `sh` resolved through a PATH you control — a vendor can squat the name. Fixed in
`SSHRunner.run` by rewriting a leading `sh` argv[0] to `self.remote_shell` (default `/bin/sh`,
override `ROUTER_REMOTE_SHELL`). General rule for this relay family: a tool that "succeeds" (rc 0)
with wrong-shaped output is worse than one that errors — a security tool returning blanks reads as
"nothing to see." When a whole *class* of tools (everything routed through one helper) goes quiet,
suspect the shared path, not each tool.

## `run`'s 300 s timeout is a hard cap, not a hint — long ops need background+poll

`register_run_tool` (`common/mage_hands_core/exec.py:269`) hard-codes `timeout=300` (and the
exec-token TTL at line 268 is the same 300 s). That's plenty for inspection commands — it's
catastrophic for a real deploy. Live example from a `reaped-whirlwind` kappa deploy:
`docker-compose -p reaped-whirlwind up -d --build inference alerting` pulls the torch CPU wheel
(~200 MB) and builds two images. On kappa that takes ~7 min. The relay returned
`Command [...] timed out after 300 seconds` while the build was still running on the host — and a
naive retry would race it. (Around 60 s after the relay gave up, the new containers transitioned
from `State: created` to `Up X seconds (healthy)`.)

**Lesson:** for any `run()` likely to exceed ~4 minutes, do not invoke it foreground. Background
it on the target and write a known log path, then poll via separate `run()` calls:

```sh
nohup sh -c 'docker-compose -p reaped-whirlwind up -d --build inference alerting; \
             echo exit=$? > /tmp/build.done' > /tmp/build.log 2>&1 < /dev/null &
disown 2>/dev/null
```

Then poll: `tail /tmp/build.log` and `list_containers` until the new containers show
`Status: Up X seconds (healthy)`. The `register_run_tool` `timeout` and `ttl` parameters are
already plumbed — if you want to lift the cap properly, wire them to `RUN_TIMEOUT` / `RUN_EXEC_TTL`
env vars in `synology-hands/server.py` and bump them for the relay instance that drives heavy
deploys. Tradeoff: a longer hard cap also means a runaway command can sit longer; background-and-
poll caps Claude's blocking time without changing the host-side ceiling.

## When the relay user needs docker without going through the relay

The relay container drives the host via `nsenter -t 1` as root, so it always has docker. But the
relay's *host* user (`magehands` on kappa) is a separate identity — useful for
`ssh magehands@nas 'docker ps'` from the Mac, which bypasses the relay (lower latency, no
exec-token dance) for inspection work. By default that user has neither the docker socket group
nor `/usr/local/bin` on its non-interactive ssh PATH, so `docker: command not found` is the first
symptom.

The fix on a Synology host (DSM 7.2) is two tweaks, both reversible:

```sh
# 1. Add the relay user to the docker group. `synogroup --member` REPLACES the member list, so
#    pass all existing members back in plus the new one. (List them via `grep ^docker /etc/group`.)
synogroup --member docker youradmin magehands

# 2. Put docker on the default non-interactive PATH. The DSM-installed binaries live at
#    /var/packages/ContainerManager/target/usr/bin/, symlinked into /usr/local/bin — which is
#    on root's PATH and interactive-login PATH, but NOT on dropbear's non-interactive sh PATH
#    (/usr/bin:/bin:/usr/sbin:/sbin). Symlink into /usr/bin to cover non-interactive too:
ln -s /usr/local/bin/docker         /usr/bin/docker
ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose
```

Group membership requires a fresh ssh session to take effect. Verify with
`ssh magehands@nas 'docker ps --format "{{.Names}}"'` — no leading absolute path.

**Lesson:** the relay's privilege model isn't the same as the relay-user's. Granting the relay's
host user `docker` socket access via the docker group is a one-line elevation that gives plain ssh
back as a tool (`docker ps`, `docker logs`, `docker exec`) without expanding the relay's tier-A/B/C
surface area. Sshd's non-interactive PATH is the standard "but it works on my login!" trap on
appliance OSes — symlink the binary into a PATH-default directory rather than chasing
`PermitUserEnvironment` (which requires an sshd reload, raising the blast radius).
