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

## A path policy must re-check what symlink resolution turned the path into

The read policy above is lexical — it normalizes `..` without touching the filesystem. But the
reader then maps the path under `/host` and **resolves symlinks** before reading, and originally
re-checked only `/host` *containment*, not the policy. A relative symlink under an allowed,
user-writable root (`/volume1/link -> ../../etc/shadow`) resolves back to `/host/etc/shadow`:
still inside `/host`, so containment passed — but the allow/deny lists only ever saw the
pre-resolution path `/volume1/link`, so the deny on `/etc/shadow` never fired. The reader now
re-runs `PathPolicy.check()` on the symlink-resolved host-absolute path. (Verified live on both
NAS boxes: the same symlink that would have leaked `/etc/shadow` now returns "denied by read
policy.") The SSH reader can't do this — it can't resolve *remote* symlinks — which is exactly
why its allow roots stay conservative and its deny list exhaustive.

**Lesson:** every layer that *transforms* a path (join, normalize, resolve symlinks) can carry it
back across a boundary an earlier check already cleared. Validate after the **last** transform,
not just the first — a lexical allow/deny list in front of a symlink-resolving reader is a lock on
a door the reader walks around.

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

## A metric that degrades to nonsense is worse than no metric

The `performance` tool computed available memory as
`mem.get("MemAvailable", mem.get("MemFree", 0))` — the ordinary defensive-fallback idiom. On
alpha it reported **161 MiB available, 99.0% used** on a box that had 13 GiB free for the asking.

`MemAvailable` entered `/proc/meminfo` in Linux 3.14. DSM 7.3 still ships **kernel 3.10.108**, so
the field is not merely sometimes-missing, it is *never present* — the fallback fired on every
call, and `MemFree` ignores reclaimable page cache, which on a NAS is most of RAM. The tool wasn't
occasionally wrong; on that entire class of host it was wrong 100% of the time, in the alarming
direction, while looking perfectly plausible.

The cost was diagnostic, not operational. The reading corroborated a *previously true* story —
the box really had been swap-thrashing — so it kept confirming a fixed problem and masked the
actual cause of a multi-day disk-churn investigation (Plex butler tasks reading 5.7 TiB). The
box's own `free -m` had the right answer the whole time.

Fix in `common/mage_hands_core/meminfo.py`: prefer the kernel's `MemAvailable`, and where it is
absent approximate it (`MemFree + Buffers + Cached - Shmem + SReclaimable`, ~1.8% optimistic vs
`free`), then **tell the caller which one it got** via `available_estimated`. Kappa's newer kernel
reports `false`; alpha reports `true`.

**Lesson:** `dict.get(key, fallback)` silently assumes the key is *usually* there. When the
fallback is semantically different from the real value, verify it's actually a fallback and not
the only path on your target platform — a `grep -c ^MemAvailable /proc/meminfo` against the real
box would have caught this at authoring time. And when a number is derived rather than measured,
put that fact *in the payload*; an operator can discount an estimate, but cannot discount a
figure that doesn't admit it's one.

## Renumbering a LAN behind an AT&T gateway in IP passthrough (2026-08-14)

When the Bayfront RT-BE92U was renumbered from factory `192.168.50.0/24` to `192.168.1.0/24` (to
preserve the fleet's on-device static IPs across the house move), every DNS lookup in the house
died while raw connectivity stayed up. Cause: an AT&T BGW620-700 in IP passthrough hands the
router a *public* WAN IP via DHCP but advertises **its own LAN address `192.168.1.254` as the DNS
server** — fine while the LAN was `.50.x` (the address simply routed out the WAN), fatal the
moment the LAN became `192.168.1.0/24` and the connected route **shadowed** the upstream
resolver. Fix: static WAN DNS on the router (`wan_dnsenable_x=0`, `1.1.1.1`/`8.8.8.8`), which
also makes the LAN immune to future BGW address weirdness.

Working the BGW's admin UI remotely from inside such a LAN:

- A `/32` host route on the ASUS must go in **Merlin's active policy table, not `main`** —
  `ip rule` shows `20: from all lookup 8437`, so it's
  `ip route add 192.168.1.254/32 dev eth0 table 8437` (a main-table twin is consulted after the
  policy table and never wins). Non-persistent; delete both after.
- The BGW login is scriptable with curl: GET `login.ha` **twice** (the form only renders once the
  session cookie exists), then POST `nonce`, `password` masked to `*`s, `hashpassword` =
  `md5(access_code + nonce)`, `Continue=Continue`. 302 = success; failures re-render with a 200.
- **The punchline: none of that is needed for routine access.** These gateways answer their UI on
  `192.168.254.254` — a hardcoded alternate management address inherited from the 2Wire/Pace
  line — reachable from any LAN client because it routes out the WAN like any other non-local
  address (verified live; a control probe to a random RFC1918 address gets nothing back).
- Changing the BGW's LAN subnet via its own UI was **silently ignored** (302 accepted, no error,
  config unchanged) — most likely locked while passthrough is active. Don't chase it.

**Lesson:** in IP passthrough the AT&T gateway is still a live DNS/DHCP actor whose private
address can collide with your LAN plan. Renumber with static upstream DNS from the start, and
try `192.168.254.254` before building route tricks to reach the gateway's page.


## A single-vendor, single-stream speedtest measures the path to that vendor, not your link

net-monitor's logs appeared to show download throughput falling from ~400 Mbps to ~256 Mbps on
the day of the Bayfront move — a plausible-looking WAN regression. It wasn't real, and it took
two independent, stacked faults to manufacture it.

**Fault 1 — AT&T's IPv4 route to Cloudflare is ~30 ms worse than its IPv6 route, at the Bayfront
address.** Measured 2026-08-24 from kappa:

| Destination | IPv4 | IPv6 |
|---|---|---|
| speed.cloudflare.com (colo DFW both families) | 43 ms | 17 ms |
| cloudflare.com | 42 ms | 21 ms |
| 1.1.1.1 | 39 ms | — |
| Google (8.8.8.8 ICMP / www.google.com connect) | 8.9 / 19 ms | 20 ms |
| Quad9 9.9.9.9 (the actual WAN resolver) | 10 ms | 10 ms |

The IPv4 traceroute to Cloudflare shows the penalty injected at the exact AT&T→Cloudflare
handoff — hop 6 `32.142.224.86` (AT&T) 18.1 ms → hop 7 `141.101.74.78` (Cloudflare) 38.0 ms →
dest 40.6 ms. The IPv6 traceroute to the same host/colo peers into Cloudflare at hop 7
(`2400:cb00:15:2::3`) in **8.985 ms**, dest 9.66 ms. Google and Quad9 over IPv4 are both fine
(~9–10 ms), so the fault is Cloudflare-and-IPv4-specific, not a general AT&T IPv4 problem. Local
gear is exonerated: the ASUS RT-BE92U adds 0.4 ms (hop 1) and the AT&T BGW620 adds 0.4 ms
(hop 2).

net-monitor's own logs independently dated the fault without any traceroute: median RTT to
1.1.1.1 was **3.7 ms for Aug 10–14** and **38.8–39.4 ms from Aug 15 onward** (the Keeler→Bayfront
house move), while 8.8.8.8 moved only 3.2 → 7.6 ms over the same window. Ten days of dated
evidence that it's Cloudflare-specific, not a local change.

**Fault 2 — net-monitor's container was IPv4-only, so it couldn't see the good path.** Its
`1.1.1.1` ping target and its Cloudflare speedtest both rode the one anomalous IPv4 path, and its
throughput test was a single 25 MB transfer — TCP slow-start at a 40 ms RTT never gets it near
line rate. The two faults compounded into a convincing but entirely false ~400 → ~256 Mbps
regression. Direct testing the same day proved the link was never degraded: 543–611 Mbps
single-stream and **1032 Mbps across 8 parallel streams**, which is kappa's own 1 GbE NIC
ceiling.

**Resolution:** documented and accepted, not fixed at the network layer. Hosts with working IPv6
already bypass the fault automatically — RFC 6724 prefers IPv6 when both are available, which is
exactly why kappa's own `curl` reached the DFW colo in 17 ms while the v4-only container crawled
at 40 ms. Explicitly considered and **rejected**:
- **Changing DNS** — unnecessary. The WAN resolver is Quad9 (`9.9.9.9`, 10 ms), not 1.1.1.1. An
  earlier memory note claiming static WAN DNS of 1.1.1.1/8.8.8.8 was wrong — `nvram` shows
  `wan0_dns = 9.9.9.9 149.112.112.112` (Quad9), and IPv6 is in fact enabled
  (`ipv6_service=dhcp6`, LAN prefix `2600:1702:63b0:139f::/64`).
- **A router-side fix** — none exists. The route is AT&T's; the BE92U and BGW between them add
  0.8 ms total.

**Lesson:** a speedtest against one vendor, over one address family, with one small transfer,
measures the path to that vendor — not your link. Stacked onto a monitoring host that itself
lacked IPv6, it couldn't even see that a better path existed, so a routing anomaly at one CDN
peering point read as a household-wide throughput collapse. Prefer multi-path, multi-stream
measurement (and diff v4 against v6 when both exist) before trusting a single-target speedtest's
verdict on "the link."


## A "neutral" third-party speedtest target needs its own hosting vetted, or it just adds a second lie

net-monitor's multi-path throughput rewrite considered adding a non-Cloudflare
`THROUGHPUT_ALT_URL`, on the theory that a third reference point would help separate "Cloudflare
peering is bad" from "the link is bad." Three candidates were measured from kappa before wiring
one in:

| Candidate | v4 connect | Result |
|---|---|---|
| aurora VM (own infra, unmetered) | 45 ms | **Rejected** — 234–620 Mbps across 5 runs of 4×75 MB, wildly variable, no local shaping visible; most likely Hetzner hypervisor-level bandwidth limiting on that VM. Best sample was no better than the Cloudflare-v4 baseline it was meant to check. |
| `ash-speed.hetzner.com` | 51 ms | Rejected — rate-limited: 107 Mbps single-stream, 266 Mbps at 4 streams. |
| `hil-speed.hetzner.com` | 208 ms | Rejected — too far to be a useful ceiling check. |

None cleared the bar (measured throughput should comfortably beat the already-known-degraded
Cloudflare-v4 path to be worth the extra daily data spend). `THROUGHPUT_ALT_URL` ships empty by
default and stays that way — the cf-v4-vs-cf-v6 comparison already is the discriminator that
found the real fault (see the entry above), at zero extra cost, and a third path only earns its
keep if it's demonstrably *not* itself hosting- or shaping-limited.

**Lesson:** a "neutral" third-party reference target isn't neutral until you've measured it under
the same conditions as the thing you're checking — a small VPS or a rate-limited public speedtest
host can be slower or more variable than the path you're trying to validate, which just adds a
second unreliable signal instead of a control. Record the rejected candidates so the next person
doesn't re-run the same dead-end experiment.


## Before blaming the router, check whether anything you own can saturate the link

Bayfront's WAN is 5 Gbps symmetrical fiber; speed tests topped out around 3.5 Gbps, and the
working hypothesis was a router CPU bottleneck on the ASUS RT-BE92U. Every layer of that turned
out to be wrong, in an instructive order.

**Nothing in the path before the client was limiting** (all read 2026-08-24):

| Hop | Reading | How |
|---|---|---|
| AT&T BGW620-700 PON | `OPERATION (O5)`, Lightspeed, **10000 Mbps full duplex** | `curl http://192.168.200.254/cgi-bin/broadbandstatistics.ha` from the dual-homed jump Pi |
| BE92U WAN `eth0` | 10GFD | `ethctl eth0 media-type` |
| BE92U LAN-side MAC `eth1` | 10GFD (SerDes) | `ethctl eth1 media-type` |
| Forwarding fast path | `Acceleration Mode: <L2 & L3>`, `runner_disable=0`, hw-switching enabled | `fc status`, `ethswctl -c hw-switching` |

Every known accelerator-killer was already off: `qos_enable=0`, `bwdpi_db_enable=0`,
`wrs_enable=0`, `TM_EULA=0` (so AiProtection has never been able to run), `MULTIFILTER_ALL=0`,
`fw_log_x=none`, no VPN client or server. Router CPU read **92.8% idle at rest**; load average
stayed 0.26–0.31 across the session. (Caveat, stated because it matters: the CPU sample was taken
*at rest*, before the throughput runs — no under-load sample was captured.)

**The 3.5 Gbps figure was an artifact of how it was measured.** The router ships
`/usr/sbin/ookla` and exposes it as the UI's Speed Test. Traffic that *terminates on* the router
gets **no flow acceleration** — the flow cache accelerates *forwarded* flows — so that test
benchmarks the CPU's TCP stack, not the forwarding capacity that carries real client traffic. The
CPU-bottleneck intuition was right about the mechanism and wrong about the consequence: it caps
the router's own speed test and says nothing about a LAN client's ceiling.

**The real ceiling is the client, and it isn't close.** Per-client PHY rates straight from the AP
(`wl -i <if> sta_info <mac>`):

| Client | Radio | PHY |
|---|---|---|
| m5 (Mac17,6, 802.11be) | 6 GHz, **160 MHz** | 2041 Mbps |
| media bridge (RT-AX88U Pro) | 5 GHz, 80 MHz (advertises SGI160) | 1200 Mbps |
| `76:fc:8f` | 5 GHz, 80 MHz | 907 Mbps |
| jump Pi | 5 GHz, 80 MHz | 433 Mbps |
| 2.4 GHz gear | 20 MHz | 52–72 Mbps |

The fastest PHY rate anywhere on the LAN is 2.04 Gbps, so 3.5 Gbps was never physically
reachable by any device in the house. Measured throughput, multi-stream to Linode Dallas +
Hetzner Ashburn: **m5 847 Mbps** down / 439 up (macOS `networkQuality`, "High" accuracy),
corroborated at 823 Mbps by an independent 8-stream `curl` run; alpha **549 Mbps** and kappa
**442 Mbps**. Treat those two as **floors, not ceilings** — they were taken against Linode and
Hetzner, and the entry above already records `ash-speed.hetzner.com` rate-limiting kappa to
107 Mbps single-stream / 266 Mbps at 4 streams. kappa is separately documented reaching
**1032 Mbps** against Cloudflare across 8 streams, which is its 1 GbE NIC ceiling. No conclusion
about either NAS's host or link capacity should be drawn from the 549/442 figures.

**Three measurement traps worth keeping:**

- **Do not sum concurrent multi-host tests behind one public IP.** Running m5 + alpha + kappa at
  once totalled **947 Mbps — less than m5 alone managed (823)**. All three egress from a single
  NAT address and the public speedtest endpoints throttle per source IP, so the hosts cannibalise
  each other. The result is worthless as an aggregate; discard it rather than reason from it.
- **`speed.cloudflare.com/__down` returned HTTP 403 to macOS `curl`**, with and without a browser
  User-Agent. Linode Dallas (`speedtest.dallas.linode.com/garbage.php?ckSize=2048`) and
  `ash-speed.hetzner.com/1GB.bin` both answered fine. *Not* checked from kappa or from
  net-monitor's container, so whether net-monitor's Cloudflare throughput probe is affected is an
  open question — verify before assuming its `tput` series is still valid.
- **`ash-speed.hetzner.com` gave 538 Mbps single-stream from m5**, against the 107 Mbps recorded
  in the entry above when measured from kappa. That rejection was source- and time-specific, not
  a fixed property of the host — re-measure before reusing a past verdict on a speedtest target.

**And one router-side oracle that simply lies:** `ATE Get_WanLanStatus` returned
`W0=T;L1=X;L2=X;L3=X;L4=X`, claiming all four LAN ports were down while the ARP table was full of
wired devices. `ethswctl -c getlanall` is no better — it reports only the aggregate
(`LAN All (count 1): eth1`). Per-physical-port link speed was not obtainable from the router at
all; the 2.5G port figure came from the owner, not from the device.

**Radios were left alone, deliberately.** 6 GHz auto-selected `channel 63, 320 MHz` and is already
maximal — m5 negotiates only 160 MHz because *Apple's Wi-Fi 7 caps there*, which no AP setting
changes. 5 GHz is pinned to `36/80`; `wl -i wl1 chanspecs -b 5 -w 160` lists `36/160` through
`128/160` and **every one of them spans DFS channels** (there is no non-DFS 160 MHz in US/201).
The only 160-capable client is the media bridge, which backhauls TV / Apple TV / Sonos and needs
under 100 Mbps — so widening would trade radar-triggered dropouts on the TV path for headroom
nothing uses. Also note: **the jump Pi is a 5 GHz client and is the only SSH path to the router**,
so `restart_wireless` cuts remote access until it reassociates.

**Lesson:** "the link is slower than the plan" is a claim about a *path*, and the client is part
of the path. Before profiling the router, enumerate what each client can physically do — an AP
association table gives you every device's PHY ceiling in one command. Here the fastest client
could use 17% of the purchased bandwidth, which no amount of router tuning would have changed,
and the number that started the investigation came from a test that never touched the forwarding
path at all.


## Enabling IPv6 on DSM buys you outbound only — the firewall's v4 rules don't translate

alpha was the last IPv4-only host on the LAN, still paying the ~30 ms AT&T→Cloudflare IPv4
penalty documented above (kappa had IPv6 all along). Enabling it is a two-line config change plus
a live apply that needs no network restart:

```sh
# /etc/sysconfig/network-scripts/ifcfg-bond0 — mirror what already worked on kappa
IPV6INIT=auto_dhcp        # was: off
IPV6_ACCEPT_RA=1
# slaves (ifcfg-eth0, ifcfg-eth1) take IPV6INIT=dhcp + IPV6_ACCEPT_RA=1

# apply without bouncing the interface (IPv4, Plex, the *arr stack all stay up)
echo 0 > /proc/sys/net/ipv6/conf/bond0/disable_ipv6
echo 1 > /proc/sys/net/ipv6/conf/bond0/autoconf
echo 2 > /proc/sys/net/ipv6/conf/bond0/accept_ra   # 2, so it survives forwarding being on
```

SLAAC produced a GUA and a default route within seconds, and the payoff was immediate: **1.1.1.1
at 38.8 ms over v4 vs 8.6 ms to the same anycast service over v6.**

**The non-obvious part is the firewall.** `synofirewall` builds the `ip6tables` chains *by itself*
the moment the v6 stack comes up — no `firewall_reload` needed, the chains were already populated
on first inspection. But **rules whose source is an IPv4 netmask do not translate into the v6
chains.** alpha's allow-list is four netmask rules (LAN, tailnet CGNAT, two docker ranges) ahead
of a final drop; what survives into `INPUT_FIREWALL` under v6 is only:

```
-A INPUT_FIREWALL -i lo -j ACCEPT
-A INPUT_FIREWALL ... ipv6-icmp types 130/133/134/135/136/137 -j ACCEPT   # ND, must not be broken
-A INPUT_FIREWALL -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT_FIREWALL -j DROP
```

So a DSM box with IPv6 enabled and the firewall on is **outbound-only**: it reaches the internet
over v6 (which is the point), and accepts no inbound v6 at all — including from your own LAN.
kappa has run exactly this way, unnoticed, for as long as it has had IPv6. The one exception is a
rule whose `source_ip_group` is `all`, which *does* materialise in v6 — alpha's `transmission-peer`
rule puts 51413 tcp/udp into the v6 chain too, though the router's `ip6tables FORWARD` only accepts
`br0 → eth0`, so it is not reachable from the WAN.

Two consequences for this repo. First, `firewall_rules` reports `synofirewall --enum IPV4` only, so
its `generated_iptables` field is silent about all of the above — read `ip6tables -S` directly when
v6 is in play. Second, if inbound v6 is ever wanted on a DSM host, it needs an explicit rule with a
v6 source (or `all`), and the delegated prefix is dynamic, so pinning one to today's `/64` is a
maintenance trap.

**Related, and a hard stop:** the guest network cannot have IPv6 here. AT&T's BGW in IP passthrough
delegates exactly one `/64` — setting `ipv6_prefix_len_wan=60` and restarting `dhcp6c` logged
`WAN Prefix Size Requested:/60, Received:/64` — and Asuswrt assigns that single `/64` entirely to
`br0`. The legacy guest bridge (`br56`, its own `dnsmasq-5.conf` with zero v6 directives, no
`br56` rule in `ip6tables FORWARD`) has no second subnet to receive, so guest clients stay v4-only
short of ULA + NAT66 with custom `/jffs` scripts. Reverted to `/64`; decided not worth it.

**Lesson:** enabling IPv6 on a firewalled host silently changes what "my firewall allows the LAN"
means, because address-family-specific rules quietly evaporate. Check `ip6tables -S` after turning
v6 on — the vendor tooling that generated the v4 rules may not admit the v6 chains exist.
