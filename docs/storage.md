# WD-004 inbox and project storage

The `storage` module is a local library, not a running collector. Use a registered
project's data directory from `user_paths().project_data(project.id)`, outside the
source checkout. It uses the standard library and adds no runtime dependencies.

```python
from agent_watchdog.config import user_paths
from agent_watchdog.storage import Inbox, Store

# event is an already normalized Envelope for a registered project.
root = user_paths().project_data(event.project_id)
inbox = Inbox(root)
inbox.publish(event)
with Store(root, event.project_id) as store:
    result = inbox.drain(store)
```

## Delivery and replay

- `publish` validates and serializes the whole envelope, flushes a sibling `.tmp`
  file with fsync, then renames it to a unique delivery `.json` file. Repeated
  deliveries never overwrite one another. The default serialized envelope limit
  is 1 MiB; larger records are rejected before creating files.
- `drain` requires an open writer for the same directory. It processes up to 100
  files per call by default and ignores unfinished `.tmp` files. Filesystem order
  is not an event-time ordering guarantee.
- Persisted records must explicitly contain `schema_version`, `event_id`, and
  `received_at`. Construction defaults must never create new identities on replay.
- Each accepted event is inserted in a SQLite transaction. The inbox file is
  removed only after COMMIT. A crash before COMMIT leaves the event for retry; a
  crash after COMMIT but before acknowledgement leaves a duplicate delivery.
- The event UUID is the primary key. Identical canonical envelopes and artifact
  references return a duplicate result. Reuse of a UUID with different content
  is rejected, never silently overwritten. There is no text-based deduplication
  of distinct event UUIDs or inferred native-event deduplication.
- SQLite and filesystem failures propagate to the caller; the unacknowledged
  input remains retryable. Earlier items in that batch may already be committed.
  Fail-open adapter behavior belongs to WD-006, not this storage primitive.

## Database and writer ownership

`Store` holds a nonblocking OS lock on `writer.lock` until its context closes.
The lock file stays in place; process exit releases the handle. A second Store
writer raises `WriterBusy`. This protects cooperating local writers; it is not
a security boundary against another application writing the database directly.
The per-user daemon lock remains WD-005.

`events.sqlite3` uses WAL, synchronous FULL, and foreign-key enforcement. Schema
version 0 is migrated to version 1 only for an empty database, in one transaction.
The metadata table binds it to a project UUID. Unsupported versions, unrelated
unversioned databases, and a mismatched project UUID are refused before changing
journal settings or applying migrations. The current release needs only the
initial migration; it has no migration framework.

`Store.events(limit=100, offset=0)` returns a bounded page in insertion order
(maximum page size 1000). `connection` exposes the active SQLite connection for
local diagnostics and tests; callers must not bypass the transaction protocol.
Independent read-only SQLite connections can read while the writer is open.
The CLI read-only reporting interface remains WD-008.

## Quarantine and artifacts

Malformed/oversized envelopes, missing replay identity, unknown envelope schemas,
project mismatches, and conflicting UUIDs do not block later input. Retain rejected
raw files under `quarantine/*.bad`, bounded by 128 files and 8 MiB by default.
Evict the oldest retained files before adding a new one. A file larger than the
entire quarantine budget is discarded. Symlink inputs are unlinked without
reading their targets. Limits are constructor arguments until user-facing quota
configuration is completed in WD-007.

`DrainResult` reports inserted events, duplicates, quarantine admissions, and
discards (including evictions). Admissions are not the final retained file count.
These are per-call results; persistent loss accounting remains WD-007.

`Store.put(event, artifacts={"stdout": content_bytes})` optionally stores artifacts.
Names are database labels, never filesystem paths. Total artifact bytes per call
are limited to 8 MiB by default. Filenames use SHA-256; identical content is shared
within a project. Complete artifact files are written before committing the event
and its references together. `read_artifact` checks size and content hash and never
accepts an arbitrary caller-supplied path. Inbox files carry envelopes only; later
ingestion can extract large fields and pass artifacts to Store directly.

## Deliberate limits and verification

This does not provide retention, total project/inbox quotas, redaction, automatic
artifact extraction, or cleanup of crash-leftover `.tmp` and unreferenced artifact
files. Those remain WD-006/WD-007. Inputs must already be suitable for local
persistence; no real provider hooks are connected by this task.

The process-crash tests terminate only their own subprocesses before event COMMIT,
after COMMIT but before inbox acknowledgement, and before migration COMMIT. They
verify rollback, replay without duplicates, and released writer ownership. Other
tests cover bad input, bounded quarantine, schema protection, project isolation,
artifact failures/integrity, and concurrent read-only access. They do not simulate
power loss or guarantee directory-entry durability across every filesystem.
Windows is tested locally; macOS/Linux host validation remains WD-019.

References: [SQLite WAL](https://www.sqlite.org/wal.html),
[SQLite schema version](https://www.sqlite.org/pragma.html#pragma_user_version),
[Windows byte-range locks](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking).
