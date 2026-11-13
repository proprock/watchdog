# Watchdog store read contract v1

`agent_watchdog.readapi` is the store interface for `wdi`. Test fixtures may
use the real writer in `agent_watchdog.capture_store`. Neither program needs
to import the daemon to read a store.

Insights runtime imports from Watchdog are limited to this read API,
`agent_watchdog.insights.llm` for explicit analysis, and `agent_watchdog._proc`
for its own process priority. Report construction, export file writing, and
pricing belong to Insights. `readapi.StorageError` is the supported error type
for project selection, unsupported databases, and session-review validation;
it is an alias of the existing storage error for compatibility.
`readapi.ConfigError` likewise aliases configuration validation errors.

## Isolated Claude analysis

`agent_watchdog.insights.llm` exposes `claude(request, forbidden_roots=..., executable=...)`
and the `Request` and `Result` types. The runner sends `Request.prompt` on stdin.
Pass `schema=None` to receive a successful `Result.output` of `{"text": answer}`.
Requests with a schema return a structured object. Failures return `Result.reason`
and no output. Callers supply checkout roots from store manifests so the runner
can refuse a working directory inside a registered checkout.
On Windows, the runner requires a Claude executable that can launch without
the command shell. Batch launchers (`.bat` and `.cmd`) are refused.

## Discovery and identity

`default_data_dir()` returns the platform default data path. Supply an explicit
data path to `stores(data_dir)` for an installation with a data override.

The daemon writes `<data>/machine_id` on first start as a UUID4. Preserve this
file with the data directory to retain the machine's identity. Each initialized
project has `<data>/projects/<project_id>/store.json`. The daemon publishes the
manifest atomically and updates it during a project visit when its metadata changes.

| Field | Value |
| --- | --- |
| `contract` | Read contract version, currently `1` |
| `machine_id` | UUID string from the machine identity file |
| `hostname`, `os` | Hostname and Python `sys.platform` value |
| `project_id` | Registered project UUID string |
| `root` | Registered absolute checkout path |
| `git_common_dir` | Absolute Git common directory or `null` |
| `name` | Saved project alias, updated after an explicit rename |
| `events_schema`, `capture_schema` | SQLite `user_version` for the respective database |
| `written_at` | ISO 8601 timestamp in UTC of the most recent manifest write |

`stores(data_dir)` returns `StoreInfo` values ordered by manifest path and
skips directories without a manifest. Each value has the manifest fields and
`path`, the project data directory. `supported` is true for the current
`CONTRACT_VERSION`. Other versions are discoverable with `supported = False`
and may have missing metadata. Invalid v1 manifests raise `ValueError`.

## Database access

`registered_projects(config_path=None, data_dir=None)` reads configuration in
registration order. Omitted locations use Watchdog's platform defaults.
Each `RegisteredProject` contains `project_id` (UUID string), `name` (saved or
generated unique alias), `root` (checkout path string), and `path` (project data
directory as a `Path`). Projects without a manifest or database remain listed;
discovery creates no directories and writes no configuration.

`resolve_project(reference=None, *, config_path=None, data_dir=None, cwd=None)`
returns one `RegisteredProject`. An explicit reference resolves aliases first,
then UUIDs. With no reference, the current directory (or supplied `cwd`) resolves
through the existing registry, including subdirectories and linked Git checkouts
sharing a registered common directory. It never auto-registers projects. Unknown
references and unregistered current directories raise `StorageError`; malformed
configuration raises `ConfigError`.

`snapshot(project)` opens a consistent events database transaction with
`mode=ro`, `query_only=ON`, and `sqlite3.Row` results. It accepts events schemas
1 through the current writer version, including stores without manifests,
and verifies the metadata project UUID. A missing database, unsupported schema,
or wrong project identity raises `StorageError`. Unlike `open_events`, this
registration-based reader uses the database schema itself, not manifest version
metadata. Keep the context short: its transaction lasts until context exit.

`findings(project)` reads one source snapshot without parsing stored envelopes
or updating materialized analysis. Missing databases return
`{"state": "unavailable", "reason": "no_collected_database"}`; schemas before
8 return `{"state": "unsupported", "reason": "schema_predates_v8"}`.
Ready results expose `state`, `window="all"`, `as_of`, `pending_sessions`, and
`by_provider` for Codex and Claude, each with `by_rule` and `gaps`.
Schema 8 reports every distinct non-null provider/session pair as pending and
no versioned findings. Schema 9 uses `session_analysis.rule_version` matching
the current analysis rules, the latest `computed_at`, and event-row watermarks
to count pending sessions. Rule counts deduplicate `session_findings.fingerprint`
per provider/rule across joined current-version session analyses. Gaps are the
sorted union of analyzed gaps, falling back to empty-analysis gaps when empty.
Findings cover all retained history, independently of usage time windows.

`session_label(db, provider, session_id)` returns `task_outcome`, `task_type`,
`progress_state`, and `reviewer_note`. Missing labels default to `unknown` and
three nulls; schemas without annotation columns return null annotations.
`session_review(project, provider, session_ids)` selects Codex or Claude sessions
in one snapshot, deduplicating identities in request order and ordering events
by source rowid. Each result has `session_id`, `label`, `event_count`, `gaps`,
and `events` (sanitized `Envelope` objects). The existing credential filter is
applied before deterministic analysis; a known manual outcome removes only
`task_outcome_unknown` from gaps. Stored envelopes remain unchanged. Empty or
blank selections, unsupported providers, and unknown sessions raise
`StorageError`. Callers write offline review bundles themselves and must retain
the review warning: credential filtering is not a general secret detector.

`open_events(info)` and `open_capture(info)` are context managers for
`events.sqlite3` and `capture.sqlite3` within `info.path`. Both open SQLite with
`mode=ro` and `query_only=ON`. An unsupported contract or a `user_version`
that differs from the manifest raises `ValueError`. `open_events` also
verifies the project ID recorded in the database. The event columns listed
below are contractual. A database that has never been created raises `FileNotFoundError`
on explicit open. SQLite may use the accompanying `-wal` and `-shm` files.

The capture database is created when capture needs it. Integer sequence
positions and record offsets are zero-based.

| Table | Columns |
| --- | --- |
| `streams` | `stream_pk`, `provider`, `session_id`, `stream_id`, `generation`, `hook_session_id`, `source_path`, `source_kind`, `cwd`, `head_len`, `head_digest`, `byte_offset`, `record_count`, `raw_bytes`, `skipped_lines`, `skipped_bytes`, `first_seen`, `last_growth`, `last_read`, `state`, `last_error` |
| `chunks` | `stream_pk`, `chunk_seq`, `first_record`, `record_count`, `raw_bytes`, `codec`, `data` |
| `tail` | `stream_pk`, `record_seq`, `line` |
| `side_files` | `provider`, `session_id`, `rel_path`, `digest`, `raw_bytes`, `codec`, `data`, `captured_at` |
| `session_env` (capture schema 2+) | `provider`, `session_id`, `captured_at`, `cwd`, `checkout_id`, `head`, `branch`, `harness`, `files`, `watchdog`, `initial_diff_digest`, `initial_diff_error` |
| `session_env` (additional columns in schema 3+) | `initial_files_changed`, `initial_insertions`, `initial_deletions`, `initial_untracked` |
| `checkout_states` (capture schema 3+) | `checkout_id`, `observed_at`, `provider`, `session_id`, `trigger`, `base_head`, `head`, `branch`, `files_changed`, `insertions`, `deletions`, `untracked`, `diff_digest`, `error` |
| `blobs` (capture schema 2+) | `digest`, `raw_bytes`, `codec`, `data` |

`streams.stream_pk` identifies a stored generation until it is purged. Refresh
the stream list after a purge, since SQLite can reuse its keys. Sealed `chunks` contain
zstd-compressed records delimited by LF. `tail.line` contains one record as a
BLOB without its LF delimiter. `side_files.data` contains the compressed bytes
of one file version identified by provider, session ID, relative path, and digest.

Capture schema 2 adds `session_env` and `blobs` under read contract 1. Use
`info.capture_schema` to determine whether the tables are available through
`open_capture(info)`. Each `session_env` row is keyed by `(provider, session_id,
captured_at)`. Its `harness`, `files`, and `watchdog` columns contain JSON.
`blobs.digest` is the SHA-256 of the uncompressed bytes, `raw_bytes` is their
length, and `data` is encoded with the named `codec` (`zstd`). Instruction and
settings blobs contain the original file bytes. File digests and
`initial_diff_digest` can exist without a matching blob when
`capture_content = false` or the store has insufficient disk headroom for the
blob. Only the first session observation carries the
initial patch result. See [Session environment snapshots](storage.md#session-environment-snapshots-wd-190)
for collection details.

Capture schema 3 adds the initial statistics and `checkout_states` under
read contract 1. The outcome key is `(checkout_id, session_id, observed_at)`.
A null statistic indicates unavailable data. A non-null outcome `diff_digest`
references a blob for the working-copy patch against the observed `head`.
Git can recover the committed changes between `base_head` and `head` while
both objects are available. See [Repository outcome snapshots](storage.md#repository-outcome-snapshots-wd-191).

## Event readers (WD-195)

`events(info, after_rowid=0)` and `event_facts(info, after_rowid=0)` yield
dictionaries ordered by `events.rowid`, strictly after the supplied cursor.
Both expose that source rowid as `event_rowid`; the facts reader joins on
`event_id`. A page releases its read transaction before yielding records.
Each iterator stops at the highest event rowid observed when reading starts;
newer events are read on the next incremental import.
A missing events database yields no records. Unsupported contracts and schema
mismatches fail explicitly, as for `open_events`. `FACT_COLUMNS` exposes the
ordered fact column names for consumers.

| Table | Contractual columns |
| --- | --- |
| `events` | `event_id`, `received_at`, `session_id`, `kind`, `envelope` |
| `event_facts` | `event_id`, `provider`, `kind`, `received_at_us`, `session_id`, `turn_id`, `turn_id_source`, `conversation_id_source`, `source`, `surface`, `checkout_id`, `agent_id`, `parent_agent_id`, `native_event_id`, `occurred_at_us`, `model`, `model_attribution`, `reasoning_effort`, `reasoning_effort_attribution`, `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`, `hook_event_name`, `tool_name`, `tool_use_id`, `tool_duration_ms`, `permission_mode`, `agent_type`, `notification_type` |

Raw event envelopes remain authoritative JSON data. Fact values are nullable;
missing usage is unavailable, never zero. Source event rowids are incremental
cursors within one store; they are not cross-machine event identities.

The event cursor is append-only, but derived facts are mutable: late Codex
context can update `model`, `model_attribution`, `reasoning_effort`, and
`reasoning_effort_attribution` on an older usage fact without changing its
event rowid. Consumers must revisit older facts to observe these changes.
Insights does this with a persisted cyclic refresh of at most 500 historical
facts per incremental import per store, using a fixed high-water mark for each
cycle. Refresh and append cursors commit atomically with the imported facts;
`wdi import --full` refreshes all facts and resets the cycle. The historical
refresh updates only the four attribution columns, leaving raw events and
token accounting unchanged. It requires no new source or Insights schema.

## Capture readers

- `streams(info)` yields `StreamInfo` with `stream_pk`, `provider`, `session_id`,
  `stream_id`, `generation`, `cwd`, `record_count`, and `raw_bytes`. It selects
  the newest generation for each `(provider, session_id, stream_id)`.
- `lines(info, stream_pk, start_record=0)` yields raw record bytes in capture
  order from the given offset up to the record count observed at iterator start.
  It removes only the LF delimiter. Offsets at or beyond that count yield no
  records. Unknown stream keys raise `KeyError`. A stream removed or replaced
  between reads raises `ValueError`.
- `side_files(info, provider, session_id)` yields `SideFileInfo` with
  `rel_path`, `digest`, `raw_bytes`, and `captured_at` for each captured version.
- `side_file_bytes(info, provider, session_id, rel_path, digest)` returns the
  original bytes of one version. Unknown keys raise `KeyError`.
- `session_env(info)` and `checkout_states(info)` yield dictionaries containing
  every source column. JSON values remain serialized. Schema 2 environment rows
  include the four initial statistics as `None`; older schemas yield no rows.
- `blob_bytes(info, digest)` returns the original bytes of a captured blob;
  missing content raises `KeyError`.

`streams` and `side_files` yield nothing if the capture database is absent.
The iterators release each read transaction before yielding results so that a
paused reader cannot hold up a WAL checkpoint. `lines` reads sealed chunks and
tail records in order even when the writer seals the tail during iteration.
Changes between pages can be visible. Callers using a raw database connection
control the lifetime of their own transactions.

## Fixture writer and compatibility

`capture_store.write_manifest(project_dir, *, machine_id, project,
events_schema=9, name=None)` publishes a manifest for a synthetic store built
through the real writer. Pass the machine UUID and a `config.Project`. By default
the manifest uses that project's alias as if it were registered alone.

Adding a table or a nullable column keeps the contract version. Removing,
renaming, or retyping a contractual field or column requires a new contract
version and a matching change to `readapi.CONTRACT_VERSION`. Manifest-based
readers refuse to open a store with a newer contract; `snapshot` validates the
registered database schema directly, as described above.
