# Driving Snapshot Replication via synowebapi (no GUI)

DSM's Snapshot Replication GUI is a thin client over local `SYNO.*` webapi calls.
Root on the box can issue the same calls with `synowebapi --exec` — which means the
whole thing is manageable through the mage-hands relay. Established empirically on
alpha (DSM 7.2, SnapshotReplication package) 2026-08-26 by creating the `books`
plan; first sync verified end-to-end. Extended 2026-08-26 with the reverse
direction (kappa → alpha) — see below.

## Ground truth locations

- **Plan state (per-plan JSON, great for monitoring):**
  `/volume1/@SnapshotReplication/plan/<plan_id>/` — `sync_report` (per-run bytes,
  duration, success), `op_report`, `plan_last_op_status`, `retention_lock_report`.
- **Config DBs:** `/var/packages/SnapshotReplication/etc/replica.db` (tables:
  `plan`, `remote_conn` (has `cred_id`, `replica_addr`, `replica_port`),
  `sync_info` (has `sched_id`), `share_replication`), plus `snap_replica.db`.
  **Back these up before create/delete calls.**
- **Reverse-direction credentials:** `/usr/syno/etc/synodr/node.db`, table
  `node_cred` (`cred_id`, `node_id`, `addr`, `port`, `protocol`, `session`). Any
  box that has ever been a replication *destination* already holds one
  auto-minted credential per plan created against it, pointing back at the
  source — see the dedicated section below.
- **Schedules are DSM scheduled tasks** (App: SnapshotReplication), one per plan,
  running `/usr/syno/bin/synosnapschedtask.sh replication <plan_id>`. Created
  automatically by plan create; inspect with `synoschedtask --get id=N`.
- **API method lists:** `/var/packages/SnapshotReplication/target/webapi/*.lib`
  (JSON). Param shapes reverse-engineered from
  `/var/packages/SnapshotReplication/target/ui/disaster_recovery.js`.

## Read calls (safe)

```sh
synowebapi --exec api=SYNO.DR.Plan method=list version=1
synowebapi --exec api=SYNO.DR.Plan method=get version=1 plan_id='"<uuid>"'
synowebapi --exec api=SYNO.DisasterRecovery.Retention method=get version=1 \
    type='"share"' name='"<share>"'
synowebapi --exec api=SYNO.Core.Share.Snapshot method=get_schedule version=1 name='"<share>"'
```

Note: complex/string params are passed as JSON, i.e. single-quoted with inner
double quotes (`plan_id='"uuid"'`). Bools/ints go bare (`nowait=true`).

## Creating a replication plan (the verified recipe)

Reuses the existing site pairing + stored credential — no passwords. Get
`cred_id`, `replica_addr`, `replica_port` from an existing plan's row in
`replica.db` `remote_conn` (or the `plan_db_record` file).

```sh
synowebapi --exec api=SYNO.DR.Plan method=create version=3 \
  nowait=true auto_remove=false is_to_local=false solution_type=1 \
  dst_volume='"/volume1"' \
  target='{"target_id":"<share>","target_type":2}' \
  src_to_dst_conns='[{"cred":{"conn":{"addr":"<dest-ip>","port":5001,"protocol":"https"},"auth":"cred_id","cred_id":"<cred-uuid>"},"replica_conn":{"replica_addr":"_AUTO_FILL_","replica_port":5566,"replica_type":2}}]' \
  sync_policy='{"enabled":true,"mode":2,"schedule":{"date_type":0,"week_name":"1,2,3,4","hour":3,"min":0,"last_work_hour":0,"repeat_hour":0,"repeat_min":0},"notify_time_in_min":720,"worm_lock_enable":false,"worm_lock_day":7,"sync_window":{"enabled":false,"window":[16777215,16777215,16777215,16777215,16777215,16777215,16777215]},"is_send_encrypted":false,"is_sync_local_snapshots":false}'
```

- `target_type` 2 = shared folder; `replica_type` 2 = btrfs.
- `schedule.week_name`: comma list, 0=Sun … 6=Sat. Hour/min are the run time.
  For weekly-once, put a single day in `week_name`.
- `sync_window` all-`16777215` = no window restriction (24×7 allowed).
- Returns `{"data":{"task_id":"@administrators/…"}}` — async. Poll:

```sh
synowebapi --exec api=SYNO.DR.Plan method=get_poll_task version=1 task_id='"<task_id>"'
```

`finish:true` + `data.plan_id`/`remote_plan_id` = done. The create automatically:
creates the remote replica share (same name, or `-1` suffix on collision), clones
a source retention policy, and installs the DSM schedule task. The GUI
additionally sets destination retention post-create
(`SYNO.DR.Plan relay` → `SYNO.DR.Plan.DRSite edit retention_policy`); the books
plan worked without touching it (defaults applied).

`get_poll_task`'s useful fields (`finish`, `data.plan_id`) are at the **head** of
the response, with the echoed input params printed below them — pipe to
`head -30`, not `tail`, or you'll read past the answer into your own request.

## Reverse-direction plans need no new credentials

Any box that has ever been a replication *destination* already holds
auto-minted reverse credentials toward its source, one per plan created against
it, stored in `/usr/syno/etc/synodr/node.db` table `node_cred` (`cred_id`,
`node_id`, `addr`, `port`, `protocol`, `session`). This means a "create the
mirror-image plan the other way" task needs no new pairing or password —
just validate an existing cred and bootstrap the create call with it:

```sh
# validate: success for a local cred, error 516 for a foreign one
synowebapi --exec api=SYNO.DR.Credential method=test_cred_id version=1 cred_id='"<uuid>"'
```

Pass the validated `cred_id` in `SYNO.DR.Plan create`'s `src_to_dst_conns` as
above — but note it's only a **bootstrap**: `create` mints a *fresh* `cred_id`
for the new plan rather than reusing the one passed in, so don't expect the
input cred to show up in the new plan's `remote_conn` row.

`SYNO.DR.Credential set` / `reverse_set` remain unexercised — those are only
needed for a genuinely unpaired box (one that has never been a source or
destination toward the other).

**Verified 2026-08-26:** reverse plan kappa `docker` share → alpha, plan_id
`ab8377d2-f68e-449b-85f3-a71efa6593fb`, daily 03:30. The replica landed as
`docker-1` on alpha — the predicted collision suffix, since alpha already has
its own `docker` share.

**Naming trap:** both boxes now have a real `docker` share *and* a `docker-1`
replica share — mirror-image plans running in opposite directions with
identical share names. Double-check which box and which plan you're looking at
before touching either; `SYNO.DR.Plan get` on the wrong `plan_id` will look
plausible right up until you delete or edit the wrong one.

`plan_last_op_status` shows a transient `err_code 407 / ERR_UNKNOWN` right
after create, before the first sync has run — that's stale create-op state,
not a failure. Poll `sync_report` instead of trusting the first read of
`plan_last_op_status`.

## Manual sync / other ops

```sh
synowebapi --exec api=SYNO.DR.Plan method=sync version=1 plan_id='"<uuid>"' \
    nowait=true is_send_encrypted=false description='"manual sync"'
# also: delete, edit, pause, stop — same api, method names per SYNO.DR.Plan.lib
```

Verify a sync by reading `sync_report` (source box) and confirming the snapshot
on the destination: `btrfs subvolume list -s /volume1 | grep <share>` — replica
snapshots live at `@sharesnap/<share>/GMT-….`

## Local (non-replicated) share snapshots

`SYNO.Core.Share.Snapshot` (in DSM core, `SYNO.Core.Share.lib`): `set_schedule` /
`get_schedule`, `set_share_conf` / `get_share_conf`, `create` / `list` / `delete`.
Schedule object shape = the `schedule` block above. Note: shares covered by a
replication plan have their *own* snapshot schedule disabled — the plan takes the
snapshots.

## Gotchas

- `SYNO.DR.Node method=list` and `SYNO.DisasterRecovery.Retention method=list`
  return error 103 — those methods don't exist; use the shapes above.
- `SYNO.DR.Plan export`/`import` is for seeding initial copies via external
  media, not config cloning.
- Error 401 from `get_poll_task`-style calls usually means missing params, not auth.
- Reverse-direction credentials: resolved — see "Reverse-direction plans need no
  new credentials" above. `SYNO.DR.Credential set` / `reverse_set` are still
  unexercised; they'd only be needed for a box that's never been paired with the
  target in either direction.

## Deleting a DSM Task Scheduler task via webapi

```sh
synowebapi --exec api=SYNO.Core.TaskScheduler method=delete version=2 \
    tasks='[{"id":13,"real_owner":"root"}]'
```

`tasks` is an array of `{id, real_owner}` objects (batch-delete capable — pass
more than one). Gotcha: a task whose owning package was **uninstalled** is
**hidden** from `method=list` (and from the Task Scheduler GUI) but still
exists and still runs. `synoschedtask --get id=N` shows it regardless, and
delete-by-id works even though `list` never surfaced it. Always
`synoschedtask --get id=N` first to confirm the task's name before deleting —
IDs get reused across packages over the life of a box, and deleting the wrong
one is silent (no "are you sure").

## Time Machine / share quotas (kappa)

Per-share, per-user quotas are set with `/usr/syno/sbin/synosharequota`, not a
separate quota database:

```sh
/usr/syno/sbin/synosharequota set-user <share> <user> <MB>
/usr/syno/sbin/synosharequota set <share> <MB>
```

It writes straight to btrfs — `usrquota rfer_hard` on the share's subvol /
qgroup `max_rfer` — so there's nothing else to keep in sync.

**Gotcha:** `set-user` **silently no-ops for members of the administrators
group** — it prints success but writes nothing, and the user keeps unlimited
share access. Confirmed on kappa: `mysterice` (an administrators member)
printed success but kept limit 0, while the non-admin `m1tm` and `timemachine`
accounts set fine. Fix by writing the btrfs quota directly, which DSM's own
read path then picks up correctly:

```sh
btrfs qgroup limit <bytes> /volume1/<share>          # or:
btrfs usrquota limit -U <user> <bytes> /volume1/<share>
```

Synology's patched `smbd` advertises the quota natively over SMB once it's
set — verify with `smbclient //host/share -U user%pw -c ls`; "blocks
available" in the output should equal the quota, not the share's full free
space.

btrfs charges quota usage to the file's **owner**, not the share it lives
under — Time Machine bands written while authenticated as the wrong account
count against *that* account's quota, not the intended one. Double-check which
user actually owns the existing sparsebundle before assuming a quota change
will bite.
