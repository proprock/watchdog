"""Local inbox and one SQLite writer per project. No daemon or hook installation."""

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_watchdog import privacy, resources
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.files import atomic_write as atomic_write


class StorageError(ValueError):
    """Unsupported storage or an invalid storage operation."""


class WriterBusy(StorageError):
    """Another writer holds the project lock."""


class RejectedEvent(StorageError):
    """Input cannot be accepted; quarantine it without stopping the queue."""


class QuotaExceeded(StorageError):
    """No room for another write; retained input may be retried after cleanup."""


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    """Keep the lock file in place; closing its handle releases ownership."""
    # Do not retry a failed initialization write implicitly when closing a buffer.
    with path.open("a+b", buffering=0) as stream:
        if os.name == "nt":
            import msvcrt

            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise WriterBusy("Project writer is unavailable") from error
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise WriterBusy("Project writer is unavailable") from error
        yield


def canonical(event: Envelope) -> str:
    validated = privacy.sanitize(event)
    document = validated.model_dump(mode="json")
    # Pre-telemetry envelopes did not have this optional field.  Omitting an
    # empty value keeps their canonical replay document and receipt fingerprint
    # byte-for-byte compatible after model validation fills the default.
    if not document["delivery"]:
        document.pop("delivery")
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def persisted_envelope(document: str | bytes) -> Envelope:
    event = Envelope.model_validate_json(document)
    if not {"event_id", "received_at", "schema_version"} <= event.model_fields_set:
        raise RejectedEvent("Persisted envelope is missing replay identity or schema")
    return event


class Store:
    def __init__(
        self,
        root: Path,
        project_id: UUID,
        *,
        artifact_bytes: int = 8 * 1024**2,
        limits: Limits | None = None,
    ):
        if artifact_bytes <= 0:
            raise StorageError("Artifact limit must be positive")
        self.root = root.resolve()
        self.project_id = project_id
        self.artifact_bytes = artifact_bytes
        self.limits = limits or Limits()
        self._connection: sqlite3.Connection | None = None
        self._stack = ExitStack()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("Store must be opened with a context manager")
        return self._connection

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise StorageError("Store is already open")
        self.root.mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        try:
            stack.enter_context(writer_lock(self.root / "writer.lock"))
            connection = sqlite3.connect(
                self.root / "events.sqlite3", isolation_level=None, timeout=2
            )
            stack.callback(connection.close)
            self._connection = connection
            with resources.admission(self.root):
                self._initialize()
        except BaseException:
            self._connection = None
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(self, *args: object) -> None:
        self._connection = None
        self._stack.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def _initialize(self) -> None:
        db = self.connection
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            raise StorageError("Unsupported database schema; database left unchanged")
        if version == 0:
            tables = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
            if tables.fetchone() is not None:
                raise StorageError("Refusing to adopt an unversioned nonempty database")
            if resources.available(self.root, self.limits) < 256 * 1024:
                raise QuotaExceeded("Insufficient space to initialize storage")
        else:
            owner = db.execute("SELECT project_id FROM metadata").fetchall()
            if owner != [(str(self.project_id),)]:
                raise StorageError("Database belongs to a different project")
            db.execute("SELECT event_id, envelope FROM events LIMIT 0")
            db.execute("SELECT event_id, name, digest, size FROM artifacts LIMIT 0")
        # Check compatibility before changing persistent journal settings or running migration.
        if version == 0:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA busy_timeout=50")
        db.execute("PRAGMA journal_size_limit=0")
        if not (self.root / "losses.bin").exists():
            resources.count_loss(self.root, "io", 0)
        if version == 0:
            with self._transaction():
                db.execute("CREATE TABLE metadata (project_id TEXT NOT NULL)")
                db.execute("INSERT INTO metadata VALUES (?)", (str(self.project_id),))
                db.execute(
                    "CREATE TABLE events (event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
                    "session_id TEXT, kind TEXT NOT NULL, envelope TEXT NOT NULL)"
                )
                db.execute("CREATE INDEX events_session ON events(session_id, received_at)")
                db.execute(
                    "CREATE TABLE artifacts (event_id TEXT NOT NULL REFERENCES events(event_id), "
                    "name TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, "
                    "PRIMARY KEY(event_id, name))"
                )
                db.execute("PRAGMA user_version=1")
        if version < 2:
            with self._transaction():
                db.execute("CREATE TABLE pins (session_id TEXT PRIMARY KEY)")
                db.execute(
                    "CREATE TABLE receipts (event_id TEXT PRIMARY KEY REFERENCES events(event_id), "
                    "digest TEXT NOT NULL)"
                )
                for event_id, document in db.execute("SELECT event_id, envelope FROM events"):
                    refs = dict(
                        db.execute(
                            "SELECT name, digest FROM artifacts WHERE event_id=?", (event_id,)
                        )
                    )
                    db.execute(
                        "INSERT INTO receipts VALUES (?, ?)",
                        (event_id, self._fingerprint(document, refs)),
                    )
                db.execute("PRAGMA user_version=2")
        if version < 3:
            with self._transaction():
                db.execute(
                    "CREATE TABLE transcript_sources ("
                    "provider TEXT NOT NULL, session_id TEXT NOT NULL, path TEXT NOT NULL, "
                    "last_seen TEXT NOT NULL, reader TEXT, device INTEGER, inode INTEGER, "
                    "size INTEGER, mtime INTEGER, "
                    "offset INTEGER NOT NULL DEFAULT 0, tail BLOB NOT NULL DEFAULT X'', "
                    "counters TEXT, last_error TEXT, error_signature TEXT, "
                    "PRIMARY KEY(provider, session_id, path))"
                )
                db.execute("PRAGMA user_version=3")
        if version < 4:
            with self._transaction():
                db.execute(
                    "CREATE TABLE diff_snapshots ("
                    "snapshot_id TEXT PRIMARY KEY, checkout_id TEXT NOT NULL, "
                    "fingerprint TEXT NOT NULL, byte_count INTEGER NOT NULL, "
                    "observed_at TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX diff_snapshots_checkout "
                    "ON diff_snapshots(checkout_id, observed_at)"
                )
                db.execute("PRAGMA user_version=4")
        if version < 5:
            with self._transaction():
                db.execute(
                    "CREATE TABLE session_labels ("
                    "provider TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "task_outcome TEXT NOT NULL, task_type TEXT, "
                    "PRIMARY KEY(provider, session_id))"
                )
                db.execute("PRAGMA user_version=5")
        # Existing v1 stores need a one-time rebuild to support physical reclamation.
        if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            size = (self.root / "events.sqlite3").stat().st_size
            if resources.available(self.root, self.limits) < 2 * size:
                raise QuotaExceeded("Insufficient migration space")
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("VACUUM")

    @staticmethod
    def _fingerprint(document: str, references: Mapping[str, str]) -> str:
        # Delivery telemetry is intentionally mutable: a replay can reach the
        # inbox/SQLite stages at a different instant without changing the
        # semantic observation identified by event_id.
        fingerprint_document = json.loads(document)
        if isinstance(fingerprint_document, dict):
            fingerprint_document.pop("delivery", None)
        return hashlib.sha256(
            json.dumps(
                [
                    json.dumps(fingerprint_document, sort_keys=True, separators=(",", ":")),
                    sorted(references.items()),
                ]
            ).encode()
        ).hexdigest()

    def put(self, event: Envelope, *, artifacts: Mapping[str, bytes] | None = None) -> bool:
        with resources.admission(self.root):
            return self._put(event, artifacts=artifacts)

    def _put(self, event: Envelope, *, artifacts: Mapping[str, bytes] | None = None) -> bool:
        event = privacy.sanitize(event)
        document = canonical(event)
        if len(document.encode()) > self.limits.payload_bytes:
            raise RejectedEvent("Envelope exceeds payload limit")
        if event.project_id != self.project_id:
            raise RejectedEvent("Event belongs to a different project")
        artifacts = artifacts or {}
        if any(
            not isinstance(name, str) or not name or not isinstance(data, bytes)
            for name, data in artifacts.items()
        ):
            raise StorageError("Artifacts require nonempty names and bytes")
        if sum(len(data) for data in artifacts.values()) > self.artifact_bytes:
            raise StorageError("Artifact limit exceeded")
        try:
            sanitized = {
                privacy.text(name): privacy.artifact(data) for name, data in artifacts.items()
            }
            if len(sanitized) != len(artifacts):
                raise StorageError("Artifact names collide after redaction")
            artifacts = sanitized
            if sum(map(len, artifacts.values())) > self.artifact_bytes:
                raise StorageError("Redacted artifact limit exceeded")
        except UnicodeDecodeError as error:
            raise StorageError("Only UTF-8 text artifacts are supported") from error
        references = {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()}
        event_id = str(event.event_id)
        fingerprint = self._fingerprint(document, references)
        existing = self.connection.execute(
            "SELECT digest FROM receipts WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing:
            if existing[0] != fingerprint:
                raise RejectedEvent("Event UUID conflicts with existing content")
            return False
        # Budget both database pages and their WAL copies, plus transaction overhead.
        needed = 65536 + 4 * len(document.encode()) + 2 * sum(map(len, artifacts.values()))
        if resources.available(self.root, self.limits) < needed:
            self._maintain(datetime.now(UTC), needed)
        if resources.available(self.root, self.limits) < needed:
            raise QuotaExceeded("Project quota or disk reserve reached")
        with self._transaction() as db:
            directory = self.root / "artifacts"
            if directory.is_symlink() or directory.is_junction():
                raise StorageError("Linked artifact directory")
            for name, data in artifacts.items():
                path = self.root / "artifacts" / f"{references[name]}.bin"
                if not path.exists():
                    atomic_write(path, data)
                else:
                    if path.is_symlink():
                        raise StorageError("Existing artifact is a symlink")
                    with path.open("rb") as stream:
                        if stream.read(len(data) + 1) != data:
                            raise StorageError("Existing artifact content is corrupt")
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (event_id, event.received_at.isoformat(), event.session_id, event.kind, document),
            )
            db.executemany(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                [(event_id, name, references[name], len(data)) for name, data in artifacts.items()],
            )
            db.execute("INSERT INTO receipts VALUES (?, ?)", (event_id, fingerprint))
            self._register_transcript_source(db, event)
        return True

    @staticmethod
    def _register_transcript_source(db: sqlite3.Connection, event: Envelope) -> None:
        """Persist any reader sources atomically with their hook envelope."""
        from agent_watchdog.transcripts import sources_from_hook

        for source in sources_from_hook(event):
            db.execute(
                "INSERT INTO transcript_sources (provider, session_id, path, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(provider, session_id, path) "
                "DO UPDATE SET last_seen=excluded.last_seen",
                (source.provider, source.session_id, source.path, event.received_at.isoformat()),
            )

    def transcript_sources(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT provider, session_id, path, last_seen, reader, device, inode, size, mtime, "
            "offset, tail, counters, last_error, error_signature FROM transcript_sources "
            "ORDER BY last_seen, provider, session_id, path"
        ).fetchall()
        names = (
            "provider",
            "session_id",
            "path",
            "last_seen",
            "reader",
            "device",
            "inode",
            "size",
            "mtime",
            "offset",
            "tail",
            "counters",
            "last_error",
            "error_signature",
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

    def update_transcript_source(
        self,
        source: Mapping[str, object],
        *,
        reader: str | None,
        device: int | None,
        inode: int | None,
        size: int | None,
        mtime: int | None,
        offset: int,
        tail: bytes,
        counters: dict[str, int | None] | None,
        last_error: str | None,
        error_signature: str | None,
    ) -> None:
        if offset < 0 or len(tail) > 1024**2:
            raise StorageError("Invalid transcript reader state")
        serialized = json.dumps(counters, sort_keys=True) if counters is not None else None
        with self._transaction() as db:
            db.execute(
                "UPDATE transcript_sources SET reader=?, device=?, inode=?, size=?, mtime=?, "
                "offset=?, tail=?, counters=?, last_error=?, error_signature=? "
                "WHERE provider=? AND session_id=? AND path=?",
                (
                    reader,
                    device,
                    inode,
                    size,
                    mtime,
                    offset,
                    tail,
                    serialized,
                    last_error,
                    error_signature,
                    source["provider"],
                    source["session_id"],
                    source["path"],
                ),
            )

    def diff_due(self, checkout_id: UUID, *, now: datetime, debounce_seconds: float = 5) -> bool:
        """Avoid invoking Git more than once per checkout debounce interval."""
        row = self.connection.execute(
            "SELECT MAX(observed_at) FROM diff_snapshots WHERE checkout_id=?", (str(checkout_id),)
        ).fetchone()
        if row is None or row[0] is None:
            return True
        try:
            previous = datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            return True
        return (now - previous).total_seconds() >= debounce_seconds

    def record_diff_snapshot(
        self,
        checkout_id: UUID,
        fingerprint: str,
        byte_count: int,
        *,
        observed_at: datetime,
    ) -> None:
        """Persist only a debounced content hash, never Git diff text."""
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise StorageError("Invalid Git diff fingerprint")
        if byte_count < 0:
            raise StorageError("Invalid Git diff byte count")
        with self._transaction() as db:
            latest = db.execute(
                "SELECT snapshot_id, fingerprint FROM diff_snapshots WHERE checkout_id=? "
                "ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                (str(checkout_id),),
            ).fetchone()
            if latest is not None and latest[1] == fingerprint:
                db.execute(
                    "UPDATE diff_snapshots SET observed_at=? WHERE snapshot_id=?",
                    (observed_at.isoformat(), latest[0]),
                )
                return
            db.execute(
                "INSERT INTO diff_snapshots VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), str(checkout_id), fingerprint, byte_count, observed_at.isoformat()),
            )

    def pin(self, session_id: str, *, pinned: bool = True) -> None:
        if not session_id.strip():
            raise StorageError("Session identity is required")
        with resources.admission(self.root), self._transaction() as db:
            if pinned:
                if not db.execute(
                    "SELECT 1 FROM events WHERE session_id=?", (session_id,)
                ).fetchone():
                    raise StorageError("Unknown session")
                db.execute("INSERT OR IGNORE INTO pins VALUES (?)", (session_id,))
            else:
                db.execute("DELETE FROM pins WHERE session_id=?", (session_id,))

    def label(self, provider: str, session_id: str, *, outcome: str, task_type: str | None) -> dict:
        if provider not in {"codex", "claude"} or not session_id.strip():
            raise StorageError("A supported provider and session identity are required")
        if outcome not in {"success", "partial", "failed", "abandoned", "unknown"}:
            raise StorageError("Invalid task outcome")
        if task_type is not None:
            task_type = task_type.strip()
            if not task_type or len(task_type) > 128:
                raise StorageError("Task type must contain 1..128 characters")
        with resources.admission(self.root), self._transaction() as db:
            if not db.execute(
                "SELECT 1 FROM events WHERE session_id=? "
                "AND json_extract(envelope, '$.provider')=?",
                (session_id, provider),
            ).fetchone():
                raise StorageError("Unknown session in the selected provider")
            db.execute(
                "INSERT INTO session_labels VALUES (?, ?, ?, ?) "
                "ON CONFLICT(provider, session_id) DO UPDATE SET "
                "task_outcome=excluded.task_outcome, task_type=excluded.task_type",
                (provider, session_id, outcome, task_type),
            )
        return {"task_outcome": outcome, "task_type": task_type}

    def pin_provider_session(self, provider: str, session_id: str, *, pinned: bool) -> None:
        if provider not in {"codex", "claude"} or not session_id.strip():
            raise StorageError("A supported provider and session identity are required")
        with resources.admission(self.root), self._transaction() as db:
            if not db.execute(
                "SELECT 1 FROM events WHERE session_id=? "
                "AND json_extract(envelope, '$.provider')=?",
                (session_id, provider),
            ).fetchone():
                raise StorageError("Unknown session in the selected provider")
            if pinned:
                db.execute("INSERT OR IGNORE INTO pins VALUES (?)", (session_id,))
            else:
                db.execute("DELETE FROM pins WHERE session_id=?", (session_id,))

    def purge_provider_session(self, provider: str, session_id: str) -> int:
        """Delete only Watchdog-owned rows for one selected provider session."""
        if provider not in {"codex", "claude"} or not session_id.strip():
            raise StorageError("A supported provider and session identity are required")
        with resources.admission(self.root), self._transaction() as db:
            event_ids = [
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM events WHERE session_id=? "
                    "AND json_extract(envelope, '$.provider')=?",
                    (session_id, provider),
                )
            ]
            if not event_ids:
                raise StorageError("Unknown session in the selected provider")
            for event_id in event_ids:
                self._delete(event_id)
            db.execute(
                "DELETE FROM session_labels WHERE provider=? AND session_id=?",
                (provider, session_id),
            )
            db.execute(
                "DELETE FROM transcript_sources WHERE provider=? AND session_id=?",
                (provider, session_id),
            )
            if provider == "claude":
                # Also drop "<parent>#<agent_id>" subagent reader rows.
                db.execute(
                    "DELETE FROM transcript_sources WHERE provider='claude' "
                    "AND session_id LIKE ? ESCAPE '\\'",
                    (
                        session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        + "#%",
                    ),
                )
            if not db.execute("SELECT 1 FROM events WHERE session_id=?", (session_id,)).fetchone():
                db.execute("DELETE FROM pins WHERE session_id=?", (session_id,))
        self._reclaim()
        return len(event_ids)

    def maintain(self, *, now: datetime | None = None) -> None:
        with resources.admission(self.root):
            self._maintain(now or datetime.now(UTC), 65536)

    def _strip_content(self, event_id: str, document: str) -> None:
        event = persisted_envelope(document)
        # Provider-specific payload may contain arbitrary content; expire all of it.
        event = event.model_copy(
            update={"payload": {}, "availability": event.availability | {"content": "unavailable"}}
        )
        self.connection.execute(
            "UPDATE events SET envelope=? WHERE event_id=?", (canonical(event), event_id)
        )
        self.connection.execute("DELETE FROM artifacts WHERE event_id=?", (event_id,))

    def _delete(self, event_id: str) -> None:
        for table in ("artifacts", "receipts", "events"):
            self.connection.execute(f"DELETE FROM {table} WHERE event_id=?", (event_id,))

    def _reclaim(self) -> None:
        db = self.connection
        referenced = {row[0] for row in db.execute("SELECT DISTINCT digest FROM artifacts")}
        directory = self.root / "artifacts"
        if directory.is_symlink() or directory.is_junction():
            raise StorageError("Linked artifact directory")
        for path in directory.glob("*.bin"):
            if (
                len(path.stem) == 64
                and all(c in "0123456789abcdef" for c in path.stem)
                and path.stem not in referenced
            ):
                path.unlink()
        # Consume all rows: SQLite may yield one row per freed page.
        db.execute("PRAGMA incremental_vacuum").fetchall()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def _maintain(self, now: datetime, needed: int) -> None:
        db = self.connection
        cutoff_content = now - timedelta(days=self.limits.content_days)
        cutoff_metrics = now - timedelta(days=self.limits.metrics_days)
        eligible = (
            "SELECT event_id, envelope FROM events WHERE session_id IS NULL OR "
            "session_id NOT IN (SELECT session_id FROM pins) ORDER BY received_at"
        )
        with self._transaction():
            for event_id, document in db.execute(eligible).fetchall():
                event = persisted_envelope(document)
                if event.received_at < cutoff_metrics:
                    self._delete(event_id)
                elif event.received_at < cutoff_content:
                    self._strip_content(event_id, document)
            db.execute(
                "DELETE FROM transcript_sources WHERE last_seen < ?", (cutoff_content.isoformat(),)
            )
        # Admission lock excludes active publishers; age also protects recent crash recovery.
        for directory in (
            self.root,
            self.root / "inbox",
            self.root / "artifacts",
            self.root / "quarantine",
        ):
            if directory.is_symlink() or directory.is_junction():
                raise StorageError("Linked data directory")
            for path in directory.glob("*.tmp"):
                if path.stat().st_mtime < now.timestamp() - 3600:
                    path.unlink()
        self._reclaim()
        if resources.available(self.root, self.limits) >= needed:
            return
        # Quota pressure: remove oldest unpinned content before whole sessions.
        for event_id, document in db.execute(eligible).fetchall():
            with self._transaction():
                self._strip_content(event_id, document)
            self._reclaim()
            if resources.available(self.root, self.limits) >= needed:
                return
        sessions = db.execute(
            "SELECT session_id, MIN(received_at) FROM events WHERE session_id IS NULL OR "
            "session_id NOT IN (SELECT session_id FROM pins) "
            "GROUP BY session_id ORDER BY MIN(received_at)"
        ).fetchall()
        for session_id, _ in sessions:
            with self._transaction():
                for (event_id,) in db.execute(
                    "SELECT event_id FROM events WHERE session_id IS ?", (session_id,)
                ).fetchall():
                    self._delete(event_id)
            self._reclaim()
            if resources.available(self.root, self.limits) >= needed:
                return

    def events(self, *, limit: int = 100, offset: int = 0) -> list[Envelope]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise StorageError("Invalid event page")
        rows = self.connection.execute(
            "SELECT envelope FROM events ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)
        )
        return [persisted_envelope(row[0]) for row in rows]

    def read_artifact(self, event_id: UUID, name: str) -> bytes:
        row = self.connection.execute(
            "SELECT digest, size FROM artifacts WHERE event_id=? AND name=?", (str(event_id), name)
        ).fetchone()
        if row is None:
            raise StorageError("Unknown artifact")
        digest, size = row
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise StorageError("Invalid artifact digest")
        path = self.root / "artifacts" / f"{digest}.bin"
        if path.is_symlink() or not 0 <= size <= self.artifact_bytes:
            raise StorageError("Invalid artifact reference")
        with path.open("rb") as stream:
            data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise StorageError("Artifact content is corrupt")
        return data


@dataclass
class DrainResult:
    inserted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    discarded: int = 0


class Inbox:
    def __init__(
        self,
        root: Path,
        *,
        payload_bytes: int = 1024**2,
        quarantine_files: int = 128,
        quarantine_bytes: int = 8 * 1024**2,
        limits: Limits | None = None,
    ):
        if min(payload_bytes, quarantine_files, quarantine_bytes) <= 0:
            raise StorageError("Inbox limits must be positive")
        self.root = root.resolve()
        self.directory = self.root / "inbox"
        self.limits = limits or Limits(payload_bytes=payload_bytes)
        self.payload_bytes = self.limits.payload_bytes
        self.quarantine_files = quarantine_files
        self.quarantine_bytes = quarantine_bytes

    def publish(self, event: Envelope) -> Path:
        content = canonical(event).encode("utf-8")
        if len(content) > self.payload_bytes:
            resources.count_loss(self.root, "payload")
            raise StorageError("Envelope exceeds inbox payload limit")
        try:
            with resources.admission(self.root):
                if self.directory.is_symlink() or self.directory.is_junction():
                    raise StorageError("Linked inbox directory")
                if (
                    resources.usage(self.directory) + len(content) > self.limits.inbox_bytes
                    or resources.available(self.root, self.limits) < len(content) + 4096
                ):
                    raise QuotaExceeded("Inbox quota or project reserve reached")
                path = self.directory / f"{uuid4().hex}.json"
                atomic_write(path, content)
                return path
        except QuotaExceeded:
            resources.count_loss(self.root, "quota")
            raise
        except WriterBusy:
            resources.count_loss(self.root, "busy")
            raise
        except OSError:
            resources.count_loss(self.root, "io")
            raise

    def _quarantine(self, path: Path, result: DrainResult) -> None:
        directory = self.root / "quarantine"
        if directory.is_symlink() or directory.is_junction():
            raise StorageError("Linked quarantine directory")
        directory.mkdir(parents=True, exist_ok=True)
        source_size = path.stat().st_size
        diagnostic = json.dumps({"reason": "rejected", "bytes": source_size}).encode()
        size = len(diagnostic)
        if size > self.quarantine_bytes:
            path.unlink()
            result.discarded += 1
            return
        entries = sorted(directory.glob("*.bad"), key=lambda entry: entry.stat().st_mtime_ns)
        total = sum(entry.stat().st_size for entry in entries)
        while entries and (
            len(entries) >= self.quarantine_files or total + size > self.quarantine_bytes
        ):
            oldest = entries.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink()
            result.discarded += 1
        # Malformed data cannot be redacted reliably. Retain only its byte count.
        if len(diagnostic) > self.quarantine_bytes:
            path.unlink()
            result.discarded += 1
            return
        atomic_write(directory / f"{uuid4().hex}.bad", diagnostic)
        path.unlink()
        result.quarantined += 1

    def drain(
        self, store: Store, *, limit: int = 100, pipeline_telemetry: bool = False
    ) -> DrainResult:
        if limit <= 0 or self.root != store.root or store.connection.in_transaction:
            raise StorageError("Invalid batch size, root mismatch, or active writer transaction")
        result = DrainResult()
        for index, path in enumerate(self.directory.glob("*.json")):
            if index >= limit:
                break
            if path.is_symlink():
                path.unlink()
                result.discarded += 1
                continue
            if not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    data = stream.read(self.payload_bytes + 1)
                if len(data) > self.payload_bytes:
                    raise RejectedEvent("Envelope exceeds inbox payload limit")
                event = persisted_envelope(data)
                if pipeline_telemetry and event.delivery:
                    observed_at = datetime.now(UTC).isoformat()
                    event = event.model_copy(
                        update={
                            "delivery": event.delivery
                            | {
                                "inbox_drained_at": observed_at,
                                "sqlite_write_started_at": observed_at,
                            }
                        }
                    )
                inserted = store.put(event)
            except (ValidationError, RejectedEvent):
                with resources.admission(self.root):
                    self._quarantine(path, result)
                resources.count_loss(self.root, "invalid")
                continue
            if inserted:
                result.inserted += 1
            else:
                result.duplicates += 1
            # If this acknowledgement fails, replay sees the same committed event UUID.
            path.unlink()
        return result
