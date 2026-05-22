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
