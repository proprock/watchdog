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
| `content_days` | 30 | Expire provider payload and artifact references; `0` disables time expiry (quota still applies) |
| `metrics_days` | 180 | Expire events and replay fingerprints; `0` disables time expiry (quota still applies) |
| `transcript_failure_minutes` | 15 | Keep a project degraded after a recent transcript-reader failure; historical gaps remain diagnostic |
| `project_bytes` | 2 GiB | Account for regular files under the project data directory |
| `inbox_bytes` | 64 MiB | Bound pending deliveries and inbox temporary files |
| `payload_bytes` | 1 MiB | Bound raw hook input and serialized envelopes |
| `reserve_bytes` | 1 MiB | Withhold room for cleanup and diagnostics |
| `log_files`, `log_bytes`, `log_level` | 5, 10 MiB, `INFO` | Global daemon diagnostic log retention and threshold |
| `log_detail` | `false` | Append a de-identified, truncated `detail="..."` error string to the diagnostic log |

All settings except the daemon-wide log settings support global defaults and project overrides. Admission checks both
remaining project budget and actual free disk space, withholding the reserve from
each. This margin is not a preallocated reservation against other applications.
Filesystem allocation overhead and unrelated writers are outside this cooperative
quota. Impractically small budgets refuse initialization or new events.

`pipeline_telemetry` is a daemon-wide configuration switch, outside `[defaults]`.
It defaults to true and records bounded delivery timestamps and queue-occupancy
samples in new envelopes. Set it to false to remove this optional hot-path work;
the spool, inbox, SQLite writer, loss counters, and their quota checks are
unchanged. It cannot be a per-project override because the native adapter has not
resolved the project when it accepts a hook.

## Daemon diagnostic log

The Python core writes best-effort, human-readable records to
`<data>/watchdog.log`. `log_bytes` caps each file; `log_files` counts the active
file, so the defaults retain `watchdog.log` and `.1` through `.4`. Rotation and
append are serialized across Python daemon and CLI processes. A busy lock or I/O
failure drops only that diagnostic record; it never changes hook stdout/stderr,
admission, or fail-open behavior.

`log_level` is global under `[defaults]` and accepts only `DEBUG`, `INFO`,
`WARNING`, or `ERROR`. Normal hot-path and quiet decisions use `DEBUG`; lifecycle,
configuration, control, drops, and degradation use higher levels. Log records use
only fixed component/event/decision/reason codes, a shape-bounded `error_type`
(an exception class name or an internal failure code) and `field` name, nonnegative
counts and byte sizes, plus Watchdog project/event UUIDs where available. By
default they never include prompts, paths, commands, provider/session IDs,
payloads, tool input/output, exception text, or tracebacks.

`log_detail` is global under `[defaults]` and defaults to `false`. When `true`, a
record produced from a caught exception gains a trailing `detail="..."` clause
holding `str(error)`: known credential forms are removed, whitespace is collapsed,
non-printable and non-ASCII characters are replaced, and the result is truncated
to 200 characters. Filesystem paths and non-credential payload fragments are kept,
so enable it only on a local development or debugging instance; keep it `false`
wherever the log may be exported or shared. It changes only the diagnostic log,
never hook output, admission, or delivery.

Transcript-enrichment failures use a fixed reader-specific `error_type` allowlist.
They keep the affected project degraded only while the source has a hook reference
within its resolved `transcript_failure_minutes` window; the persisted reader gap
outlives that health signal and is still retried if the source changes. Status
exposes only active failure codes per project, never reader paths or session IDs.
Their records are
content-free with the default `log_detail = false`; when explicitly enabled, the
same bounded, credential-redacted detail policy applies. A separate
`decision=inert reason=usage_seen_unstored` record marks the case where a reader
consumed new usage-bearing lines but stored none of them without raising a
failure; it is debounced per project and holds the project degraded until a
later pass accepts a usage observation, so a silent enrichment drop is never
mistaken for an idle poll.

Publishers and storage writes/maintenance share `admission.lock`, with a 100 ms
acquisition timeout. Pending temporary files count; SQLite admission additionally
budgets database/WAL growth and transaction overhead. Database, WAL, SHM,
artifacts, quarantine, diagnostics, lock files, and auxiliary files consume space.
Shared content-addressed artifacts count once. Pins do not waive the quota.

## Redaction and content

The local per-project store is not a sharing surface. It keeps the raw provider
input, subject to `capture_content` and size limits, so a bounded pattern matcher
never destroys the only local copy of a session. WD-115 makes the credential
filter an export-time transform: ingest and drain persist raw input, and
`inspection` applies the filter when it writes an export intended to leave the
machine. The paragraphs below describe the filter itself and the pre-WD-115
ingest behavior it replaces.

Hooks capture native `prompt`, `tool_input`, `tool_response`, and
`last_assistant_message` fields when present. Capture defaults to true for
registered projects; set `capture_content = false` globally or per project to
omit text. Codex `transcript_path` is retained as operational reader metadata
even when text capture is disabled, but enrichment stores only validated numeric
usage records and never transcript text or raw lines. Other top-level provider
fields are retained as redacted metadata even when their schema is not yet known;
this makes future model, reasoning, context, token, timing, retry, permission,
error, and lifecycle telemetry recoverable without inventing a value. New field
names are recorded in `unknown_fields` and a bounded diagnostic WARNING; nested
tool input/output keys are not treated as provider-schema changes.
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
partial tails, file identity, reader errors, and cumulative usage baselines; schema
v4 adds debounced per-checkout Git diff fingerprints and byte counts; schema v5
adds provider-scoped session outcome/type labels; schema v6 adds the
[queryable telemetry projections](#schema-v6-queryable-telemetry-projections)
described below; schema v7 adds the
[manual annotation fields](#schema-v7-manual-annotations) used by calibration. Diff text is never stored. The core runs the bounded Git read
after spool admission; hook paths do not invoke Git or snapshot a worktree.
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

## Schema v6: queryable telemetry projections

Schema v6 makes token use and process-efficiency telemetry queryable without
JSON extraction. `events.envelope` stays the authoritative, redacted document;
v6 only adds derived, nullable projections and never rewrites a stored envelope.

`events` gains one column, `provider`, so provider-scoped reads and joins do not
call `json_extract`. All other projections live in a companion table,
`event_facts`, with one row per event (`event_id` primary key referencing
`events`). `Store._put` writes the `event_facts` row in the same transaction as
its event; `_delete` removes it with the event at the metrics horizon;
`_strip_content` leaves it untouched, so aggregates survive content expiry that
would otherwise erase the source metadata.

`event_facts` copies the filter keys `provider`, `kind`, `received_at_us`
(integer epoch microseconds), `session_id`, and `turn_id` so analysis stays
single-table and partial indexes on `kind` are possible. It then holds:

- Envelope context: `turn_id_source`, `conversation_id_source`, `source`,
  `surface`, `checkout_id`, `agent_id`, `parent_agent_id`, `native_event_id`,
  `occurred_at_us`.
- Configuration: `model`, `model_attribution`, `reasoning_effort`,
  `reasoning_effort_attribution`. An attribution is `observed` when the value
  comes straight from provider metadata (or a usage reader's own
  `message.model` projection), `inherited` when a `usage` row takes the latest
  earlier observed value from the same conversation (same turn first), and
  `unavailable` otherwise. Claude reasoning effort is read from the nested
  `effort.level`; Codex from the flat `reasoning_effort` key. Claude hook rows
  carry no model, so `model_attribution` is `unavailable` there.
- Usage deltas (`usage` rows only): `input_tokens`, `cached_input_tokens`,
  `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`,
  `total_tokens`. Codex fills all six from the transcript delta. Claude maps
  `cache_read_input_tokens`/`cache_creation_input_tokens`/`thinking_tokens` onto
  the cached/cache-write/reasoning columns and leaves `total_tokens` NULL,
  because Anthropic reports no total; no column is ever a synthetic sum of the
  others.
- Process efficiency, from the provider metadata namespace only (never
  `tool_input`/`tool_response`): `hook_event_name` (with `PostToolUseFailure`
  marking a tool error), `tool_name`, `tool_use_id`, `tool_duration_ms`
  (`tool.finish` only), `permission_mode`, `agent_type`, `notification_type`.

The canonical conversation key is `events.session_id`; `conversation_id_source`
records whether it was taken from the envelope `session_id` or, for a Codex
usage row, from `payload.codex.thread_id` (equal in observed data). The turn key
is Claude's `prompt_id` (`turn_id_source = 'prompt_id'`) or Codex's `turn_id`
(`'turn_id'`). Claude hooks now also promote `prompt_id` onto the envelope
`turn_id`; this changes the canonical document and replay fingerprint for Claude
hook events written after the upgrade only. A Claude subagent `usage` row has no
turn of its own, so its `turn_id` is resolved to the parent turn by matching the
subagent `agent_id` against the parent's `agent.start` / `agent.end` events
(`turn_id_source = 'parent_agent'`); an absent match stays `'unavailable'`.

`Store._initialize` runs the v5-to-v6 step transactionally after the existing
ownership and compatibility checks, with a space pre-check like the v1 rebuild.
It adds the column and table, backfills both in `rowid` (insertion) order so the
inheritance resolution is deterministic, creates the indexes, and advances
`PRAGMA user_version` only on commit. Indexes:

| index | columns | scope |
| --- | --- | --- |
| `event_facts_context` | `provider, session_id, turn_id, received_at_us` | all rows |
| `event_facts_usage_time` | `received_at_us, provider, model, reasoning_effort` | `kind = 'usage'` |
| `event_facts_usage_dim` | `provider, model, reasoning_effort, received_at_us` | `kind = 'usage'` |
| `event_facts_tool_pair` | `provider, session_id, turn_id, tool_use_id` | `kind IN ('tool.start','tool.finish')` |
| `event_facts_tool_cost` | `provider, tool_name, received_at_us` | `kind = 'tool.finish'` |

Turn wall time, tools per turn, inter-turn latency, permission-stall time, and
tool error rate are derived at query time from these columns and `received_at`;
they get no column. Claude subagent `usage` rows carry the subagent `agent_id`,
the parent `session_id`, and the resolved parent `turn_id`, so subagent cost
joins to the parent conversation and turn. A subagent keeps its own observed
`model`; model/effort inheritance never crosses between a subagent and its
parent. The subagent transcript reader keeps `isSidechain` lines (every line of
a dedicated subagent transcript carries that flag); the skip only applies to the
parent session transcript, where those lines are a duplicate of the subagent's
own log. Monetary estimates and tariffs are out of scope and deferred.

`agent_watchdog.facts_query` provides the read-only aggregate helpers
(`token_usage`, `process_efficiency`, `coverage`) over `event_facts`; the
[`usage` CLI command](cli.md#token-and-process-telemetry) assembles them from a
read-only snapshot. Every figure is a raw `SUM`/`COUNT`; coverage is reported
beside the aggregates, never folded in.

`agent_watchdog.pricing` and `facts_query.cost` add an optional list-price
estimate over the same rows: each `usage` row is priced from its own `model` and
timestamp against a dated tariff file, never persisted and recomputed on every
call. Only `input_tokens`, `output_tokens`, `cached_input_tokens`, and
`cache_write_input_tokens` are priced; `reasoning_output_tokens` (a subset of
`output_tokens`, already billed) and `total_tokens` (an overlapping sum) are
not. A single `cache_write` rate is a blend across Anthropic's 5-minute and
1-hour cache-creation tiers, which bill at different multiples of the input
rate; v6 does not separate them, so pin the rate to the tier you use. It is a
list-price estimate, not billed spend — subscription and enterprise pricing
differ.

## Retention and pins

The daemon maintains each project on first access and at most once per minute
thereafter; storage admission also requests cleanup under quota pressure.
Retention is a disk-budget control over the user's own local data, not a privacy
control. Retention uses received time. By default unpinned provider payload and
artifact references expire after 30 days and events after 180 days;
`content_days = 0` / `metrics_days = 0` disable time-based expiry, leaving the
project quota and degraded-state loss counters as the only bound.
Payload is removed as a unit so unknown provider fields cannot retain text
past the configured window. Core event identity, kind, times, and availability
remain.

Under pressure, cleanup removes oldest unpinned content, then oldest unpinned
sessions. Null-session events form one unpinned group. Pins protect the entire
session, including later events sharing its ID, from expiry and eviction.
`store.pin(session_id, pinned=False)` removes protection. WD-011 adds
provider-scoped user-facing label, pin, and purge requests through the core plus
offline exports; legacy pin retention remains keyed by native session ID.

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

## Schema v7: manual annotations

Schema v7 stores the manual judgements a calibration review produces. It adds
nothing to the observation path: no rule reads these rows, and their absence is
the normal state.

`session_labels` gains two nullable columns, `progress_state` and
`reviewer_note`. `progress_state` holds one of `progress`, `slow`, `stuck`, or
`externally_blocked` and is a separate field from `task_outcome`, because a
session can reach `success` after being stuck and an abandoned session can have
been progressing when it stopped. Writing a label with either field omitted
keeps the stored value, so recording an outcome cannot silently discard a
progress judgement.

Two tables hold per-finding verdicts (`true_positive`, `false_positive`, or
`uncertain`). Findings are recomputed on read and have no stored identity, so a
verdict is keyed by rule, rule version, and the SHA-256 `fingerprint` over the
finding's sorted evidence identifiers. If the evidence set grows, the finding is
a different observation and its fingerprint changes rather than inheriting an
old verdict.

- `finding_verdicts(provider, session_id, rule, rule_version,
  evidence_fingerprint, verdict, note, reviewed_at)` for rules whose evidence is
  a session's own events.
- `checkout_finding_verdicts(checkout_id, rule, rule_version,
  evidence_fingerprint, verdict, note, reviewed_at)` for `diff_oscillation`,
  whose evidence is a checkout's diff history. A checkout is shared by both
  providers, so that verdict carries no provider and no session.

`purge` deletes a session's verdict rows with its label. Checkout verdicts
survive a session purge because they do not belong to a session.

## `diff_oscillation` session attribution (WD-118)

No schema change: `diff_snapshots` still stores only `checkout_id`, not a
session. The fan-out this fixed was a read-side bug, not a storage gap. A
checkout is often shared by every session that ever ran in that working copy,
so grouping oscillations by `checkout_id` alone (as `analysis.diff_oscillations`
always has) meant every session's `report` echoed the same checkout-wide
findings, whether or not that session was active when the diff actually
changed; on the WD-012 live cohort this multiplied 8 distinct oscillations
into 384 per-session instances across the 48 sessions that had ever touched
that one checkout.

`inspection.snapshots_for_checkouts` now tags each fetched snapshot with
`session_ids`: the sessions whose `turn.start`/`turn.end` window (queried
across every session sharing the checkout, from the existing `events` table)
covered the snapshot's `observed_at`. `analysis.diff_oscillations` unions the
three implicated snapshots' `session_ids` onto the finding; `analyze` then
only keeps the finding for a session in that set. Two effects fall out of
this without any new stored fact:

- A session with no open turn during any of the three snapshots no longer
  sees a finding it had nothing to do with.
- Two or more sessions with overlapping turns keep the finding for both (a
  concurrent editor within Watchdog's own observation), and the rule's
  `attribution` field stays `"uncertain"` exactly as it already did — this
  change narrows *who is told*, not the rule's honesty about *whether it was
  really that session's edit*.

An oscillation with no session at all attributable (no turn boundary observed
for the checkout, e.g. events predating turn-boundary hooks) keeps the old
inclusive behavior and is still shown to every session touching the checkout:
narrowing on missing evidence would manufacture false precision, not reduce
it. `checkout_finding_verdicts` is unaffected — the manual review of a
diff-oscillation finding is still recorded once per checkout, keyed by the
same evidence fingerprint (the finding's evidence IDs did not change).

## Verification

Offline pytest covers concurrent quotas and loss counting, pin saturation,
content/metric expiry, replay after expiry, shared artifacts, stale temporary and
orphan cleanup, physical SQLite shrink, WAL/auxiliary accounting, v1 upgrade,
the v6-to-v7 annotation upgrade, known-secret removal, and simulated disk-full
publication. Existing process-crash
tests cover migration, commit, and acknowledgement. See [verification](verification.md)
for actual runs. Other-OS host validation remains WD-019.

References: [SQLite auto-vacuum](https://www.sqlite.org/pragma.html#pragma_auto_vacuum),
[incremental vacuum](https://www.sqlite.org/pragma.html#pragma_incremental_vacuum),
[WAL checkpoints](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint).
