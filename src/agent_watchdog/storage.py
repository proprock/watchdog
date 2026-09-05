"""Local inbox and one SQLite writer per project. No daemon or hook installation."""

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_watchdog.events import Envelope
from agent_watchdog.files import atomic_write as atomic_write


class StorageError(ValueError):
    """Unsupported storage or an invalid storage operation."""


class WriterBusy(StorageError):
    """Another writer holds the project lock."""


class RejectedEvent(StorageError):
    """Input cannot be accepted; quarantine it without stopping the queue."""


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
    validated = Envelope.model_validate_json(event.model_dump_json())
    return json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def persisted_envelope(document: str | bytes) -> Envelope:
    event = Envelope.model_validate_json(document)
    if not {"event_id", "received_at", "schema_version"} <= event.model_fields_set:
        raise RejectedEvent("Persisted envelope is missing replay identity or schema")
    return event


class Store:
    def __init__(self, root: Path, project_id: UUID, *, artifact_bytes: int = 8 * 1024**2):
        if artifact_bytes <= 0:
            raise StorageError("Artifact limit must be positive")
        self.root = root.resolve()
        self.project_id = project_id
        self.artifact_bytes = artifact_bytes
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
        if version not in (0, 1):
            raise StorageError("Unsupported database schema; database left unchanged")
        if version == 0:
            tables = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
            if tables.fetchone() is not None:
                raise StorageError("Refusing to adopt an unversioned nonempty database")
        else:
            owner = db.execute("SELECT project_id FROM metadata").fetchall()
            if owner != [(str(self.project_id),)]:
                raise StorageError("Database belongs to a different project")
            db.execute("SELECT event_id, envelope FROM events LIMIT 0")
            db.execute("SELECT event_id, name, digest, size FROM artifacts LIMIT 0")
        # Check compatibility before changing persistent journal settings or running migration.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
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

    def put(self, event: Envelope, *, artifacts: Mapping[str, bytes] | None = None) -> bool:
        document = canonical(event)
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
        references = {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()}
        event_id = str(event.event_id)
        with self._transaction() as db:
            existing = db.execute("SELECT envelope FROM events WHERE event_id=?", (event_id,))
            row = existing.fetchone()
            if row is not None:
                stored = dict(
                    db.execute("SELECT name, digest FROM artifacts WHERE event_id=?", (event_id,))
                )
                if row[0] != document or stored != references:
                    raise RejectedEvent("Event UUID conflicts with existing content")
                return False
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
        return True

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
    ):
        if min(payload_bytes, quarantine_files, quarantine_bytes) <= 0:
            raise StorageError("Inbox limits must be positive")
        self.root = root.resolve()
        self.directory = self.root / "inbox"
        self.payload_bytes = payload_bytes
        self.quarantine_files = quarantine_files
        self.quarantine_bytes = quarantine_bytes

    def publish(self, event: Envelope) -> Path:
        content = canonical(event).encode("utf-8")
        if len(content) > self.payload_bytes:
            raise StorageError("Envelope exceeds inbox payload limit")
        path = self.directory / f"{uuid4().hex}.json"
        atomic_write(path, content)
        return path

    def _quarantine(self, path: Path, result: DrainResult) -> None:
        directory = self.root / "quarantine"
        directory.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size
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
        path.replace(directory / f"{uuid4().hex}.bad")
        result.quarantined += 1

    def drain(self, store: Store, *, limit: int = 100) -> DrainResult:
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
                inserted = store.put(persisted_envelope(data))
            except (ValidationError, RejectedEvent):
                self._quarantine(path, result)
                continue
            if inserted:
                result.inserted += 1
            else:
                result.duplicates += 1
            # If this acknowledgement fails, replay sees the same committed event UUID.
            path.unlink()
        return result
