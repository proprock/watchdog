# Project storage and resource policy

WD-004 provides the inbox and SQLite writer; WD-007 adds retention, redaction,
cooperative quotas, pins, and persistent loss diagnostics. No new dependency or
service is required. Data lives in the registered project's UUID directory,
outside its source checkout. Vendor transcripts are never cleanup targets; the
Codex reader opens only exact paths recorded from hooks.

```python
from agent_watchdog.storage import Inbox, Store

# root = user_paths().project_data(event.project_id)
# limits = project.overrides.apply(config.defaults)
inbox = Inbox(root, limits=limits)
inbox.publish(event)
with Store(root, event.project_id, limits=limits) as store:
    result = inbox.drain(store)
    store.pin(event.session_id)  # Requires an already observed, nonempty session.
    store.maintain()
```

## Defaults and admission

| Setting | Default | Meaning |
| --- | --- | --- |
| `capture_content` | true | Capture selected native text fields; false keeps metadata only |
| `content_days` | 30 | Expire provider payload and artifact references |
| `metrics_days` | 180 | Expire events and replay fingerprints |
| `project_bytes` | 2 GiB | Account for regular files under the project data directory |
| `inbox_bytes` | 64 MiB | Bound pending deliveries and inbox temporary files |
| `payload_bytes` | 1 MiB | Bound raw hook input and serialized envelopes |
| `reserve_bytes` | 1 MiB | Withhold room for cleanup and diagnostics |
| `log_files`, `log_bytes` | 5, 10 MiB | Reserved configuration; no append-only logger exists |

All settings support global defaults and project overrides. Admission checks both
remaining project budget and actual free disk space, withholding the reserve from
each. This margin is not a preallocated reservation against other applications.
Filesystem allocation overhead and unrelated writers are outside this cooperative
quota. Impractically small budgets refuse initialization or new events.

Publishers and storage writes/maintenance share `admission.lock`, with a 100 ms
acquisition timeout. Pending temporary files count; SQLite admission additionally
budgets database/WAL growth and transaction overhead. Database, WAL, SHM,
artifacts, quarantine, diagnostics, lock files, and auxiliary files consume space.
Shared content-addressed artifacts count once. Pins do not waive the quota.

## Redaction and content

Hooks capture native `prompt`, `tool_input`, `tool_response`, and
`last_assistant_message` fields when present. Capture defaults to true for
registered projects; set `capture_content = false` globally or per project to
omit text. Codex `transcript_path` is retained as operational reader metadata
even when text capture is disabled, but enrichment stores only validated numeric
usage records and never transcript text or raw lines. Other arbitrary native
fields are not copied.
`Inbox.publish` and `Store.put` also sanitize inputs before writing temporary or
persistent files. Artifact names cannot collide after redaction. Only UTF-8 text
artifacts are supported, capped at 8 MiB per call before and after redaction.

The deterministic filter removes credential-valued dictionary fields, common
OpenAI/GitHub/AWS access-key forms, Bearer/Basic credentials, password/token/API-key
assignments, credential URLs, and complete or truncated PEM private-key blocks.
It can over-redact and does not detect arbitrary prose secrets, all token formats,
encoded data, or split credentials. It is not anonymization and does not
retroactively sanitize data from an earlier release.

Rejected malformed/oversized envelopes, unsupported schemas, missing replay IDs,
wrong projects, and conflicting UUIDs never enter quarantine as raw text. A `.bad`
record contains only a generic reason and byte count. Quarantine is bounded by
128 files and 8 MiB; oldest diagnostics are evicted first. Input symlinks are
unlinked without reading their targets. Linked inbox/artifact/quarantine
directories are refused.

## Delivery, migration, and recovery

Publishing validates the envelope, fsyncs a sibling `.tmp`, and renames it to a
unique delivery `.json`. The event UUID stays unchanged. `drain` processes at most
100 files by default, in filesystem order, with an open Store for the same root.
Persisted envelopes must explicitly contain schema version, event UUID, and
received time. SQLite commits events and artifact references together before
acknowledging inbox files. I/O/SQLite failures leave input retryable, including a
commit/ack crash. A fingerprint of the original sanitized event and artifact
references preserves deduplication after content expiry. Changed content under
the same UUID is rejected. Fingerprints expire with the event metrics.

`writer.lock` gives one Store ownership per project. SQLite uses WAL, synchronous
FULL, foreign keys, and secure deletion of freed cells. Schema v2 adds session pins
and replay fingerprints; schema v3 adds durable Codex transcript source cursors,
partial tails, file identity, reader errors, and cumulative usage baselines.
Reader sources expire after 30 days without a hook observation; no vendor file is
deleted or modified. New databases enable incremental vacuum before creating
tables; v1 databases need a one-time VACUUM rebuild with a space check. If the
rebuild cannot finish, reopening retries it. Unknown versions, unrelated
unversioned databases, and wrong project ownership are refused before journal or
migration changes. Upgrade preserves existing content.

`Store.events(limit=100, offset=0)` returns at most 1000 events in insertion order.
`read_artifact(event_id, name)` checks size and SHA-256 and never accepts an
arbitrary path. Read-only SQLite connections may coexist with the writer.
The [project/session CLI](cli.md) is available.

## Retention and pins

The daemon maintains each project on first access and at most once per minute
thereafter; storage admission also requests cleanup under quota pressure.
Retention uses received time. Unpinned provider payload and artifact references
expire after 30 days; events expire after 180 days. Payload is removed as a unit
so unknown provider fields cannot retain text indefinitely. Core event identity,
kind, times, and availability remain.

Under pressure, cleanup removes oldest unpinned content, then oldest unpinned
sessions. Null-session events form one unpinned group. Pins protect the entire
session, including later events sharing its ID, from expiry and eviction.
`store.pin(session_id, pinned=False)` removes protection. User-facing pin, label,
and export commands remain WD-011.

Cleanup removes unreferenced hash-named artifacts and `.tmp` files older than one
hour in Watchdog-owned locations. Referenced shared artifacts and recent temporary
files survive. Incremental vacuum and WAL checkpoint reclaim disk space. A reader
can delay truncation; admission can remain degraded until it releases its
snapshot. Cleanup is not forensic erasure of backups, snapshots, or old WAL
readers. Vendor transcript directories are never recursively scanned or used as
cleanup targets; WD-009 reads only exact hook-provided files.

## Loss diagnostics and degraded state

`daemon status` includes persistent counters even when stopped: project quota,
oversized payload, invalid event, I/O, and busy-admission rejections, plus
adapter-level failures that cannot be assigned to a project. They count rejected
observations, not expiry or retries of retained deliveries. Pause and unregistered
projects are intentional exclusions.

Counters use a fixed 40-byte file and a separate short lock. Updates reuse allocated
bytes and fsync after releasing the update lock, allowing an initialized counter
to survive a simulated ENOSPC
publication failure without allocating a temporary file. Persistence is best
effort when the directory cannot be created, the lock times out, permissions are
lost, or the device refuses writes. This is not a power-loss-safe audit ledger.
Corrupt counters are reported unavailable instead of reset. Status returns at
most 32 project counter groups; direct `resources.losses(root)` reads any project.

Project write failures or exhausted capacity produce degraded daemon status. A
full inbox still triggers a missing daemon's restart so cleanup can resume. Hooks
keep returning the no-op response. Historical loss counts alone do not keep a
recovered daemon degraded.

## Verification

Offline pytest covers concurrent quotas and loss counting, pin saturation,
content/metric expiry, replay after expiry, shared artifacts, stale temporary and
orphan cleanup, physical SQLite shrink, WAL/auxiliary accounting, v1 upgrade,
known-secret removal, and simulated disk-full publication. Existing process-crash
tests cover migration, commit, and acknowledgement. See [verification](verification.md)
for actual runs. Other-OS host validation remains WD-019.

References: [SQLite auto-vacuum](https://www.sqlite.org/pragma.html#pragma_auto_vacuum),
[incremental vacuum](https://www.sqlite.org/pragma.html#pragma_incremental_vacuum),
[WAL checkpoints](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint).
