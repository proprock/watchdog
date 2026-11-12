# Watchdog store read contract v1

`agent_watchdog.readapi` is the runtime interface for `wdi`. Test fixtures may
use the real writer in `agent_watchdog.capture_store`. Neither program needs
to import the daemon to read a store.

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

`open_events(info)` and `open_capture(info)` are context managers for
`events.sqlite3` and `capture.sqlite3` within `info.path`. Both open SQLite with
`mode=ro` and `query_only=ON`. An unsupported contract or a `user_version`
that differs from the manifest raises `ValueError`. `open_events` also
verifies the project ID recorded in the database. The event tables are outside
this contract. A database that has never been created raises `FileNotFoundError`
on explicit open. SQLite may use the accompanying `-wal` and `-shm` files.

The capture database is created when capture needs it. Its v1 tables have the
following columns. Integer sequence positions and record offsets are zero-based.

| Table | Columns |
| --- | --- |
| `streams` | `stream_pk`, `provider`, `session_id`, `stream_id`, `generation`, `hook_session_id`, `source_path`, `source_kind`, `cwd`, `head_len`, `head_digest`, `byte_offset`, `record_count`, `raw_bytes`, `skipped_lines`, `skipped_bytes`, `first_seen`, `last_growth`, `last_read`, `state`, `last_error` |
| `chunks` | `stream_pk`, `chunk_seq`, `first_record`, `record_count`, `raw_bytes`, `codec`, `data` |
| `tail` | `stream_pk`, `record_seq`, `line` |
| `side_files` | `provider`, `session_id`, `rel_path`, `digest`, `raw_bytes`, `codec`, `data`, `captured_at` |

`streams.stream_pk` identifies a stored generation until it is purged. Refresh
the stream list after a purge, since SQLite can reuse its keys. Sealed `chunks` contain
zstd-compressed records delimited by LF. `tail.line` contains one record as a
BLOB without its LF delimiter. `side_files.data` contains the compressed bytes
of one file version identified by provider, session ID, relative path, and digest.

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
version and a matching change to `readapi.CONTRACT_VERSION`. A reader refuses
to open a store with a newer contract.
