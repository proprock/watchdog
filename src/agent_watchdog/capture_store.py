"""SQLite storage for byte-exact transcript capture."""

import sqlite3
from collections.abc import Iterator
from compression import zstd
from contextlib import closing, contextmanager
from io import BytesIO
from pathlib import Path
from typing import TypedDict

from agent_watchdog.storage import StorageError

CAPTURE_FILENAME = "capture.sqlite3"
SCHEMA_VERSION = 1
CHUNK_TARGET_BYTES = 4 * 1024**2
ZSTD_LEVEL = 12

_SCHEMA = (
    """
    CREATE TABLE streams (
        stream_pk INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        stream_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        hook_session_id TEXT,
        source_path TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        cwd TEXT,
        head_len INTEGER NOT NULL,
        head_digest TEXT NOT NULL,
        byte_offset INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        raw_bytes INTEGER NOT NULL,
        skipped_lines INTEGER NOT NULL DEFAULT 0,
        skipped_bytes INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        last_growth TEXT NOT NULL,
        last_read TEXT NOT NULL,
        state TEXT NOT NULL,
        last_error TEXT,
        UNIQUE (provider, session_id, stream_id, generation)
    )
    """,
    """
    CREATE TABLE chunks (
        stream_pk INTEGER NOT NULL REFERENCES streams ON DELETE CASCADE,
        chunk_seq INTEGER NOT NULL,
        first_record INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        raw_bytes INTEGER NOT NULL,
        codec TEXT NOT NULL,
        data BLOB NOT NULL,
        PRIMARY KEY (stream_pk, chunk_seq)
    )
    """,
    """
    CREATE TABLE tail (
        stream_pk INTEGER NOT NULL REFERENCES streams ON DELETE CASCADE,
        record_seq INTEGER NOT NULL,
        line BLOB NOT NULL,
        PRIMARY KEY (stream_pk, record_seq)
    )
    """,
    """
    CREATE TABLE side_files (
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        digest TEXT NOT NULL,
        raw_bytes INTEGER NOT NULL,
        codec TEXT NOT NULL,
        data BLOB NOT NULL,
        captured_at TEXT NOT NULL,
        PRIMARY KEY (provider, session_id, rel_path, digest)
    )
    """,
)


class CaptureStats(TypedDict):
    streams: int
    open: int
    missing: int
    raw_bytes: int
    stored_bytes: int
    skipped_lines: int
    last_errors: list[str]


class CaptureStore:
    def __init__(self, path: Path):
        self.path = path.resolve()

    @contextmanager
    def connect(
        self, *, write: bool = True, create: bool = True, timeout: float = 0.1
    ) -> Iterator[sqlite3.Connection]:
        """Open a transaction, with an existing-only read snapshot when write is false."""
        initialize = write and create
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc" if initialize else "rw" if write else "ro"
        uri = f"{self.path.as_uri()}?mode={mode}"
        with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as db:
            db.row_factory = sqlite3.Row
            _initialize(db, create=initialize)
            if write:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def purge(self, provider: str, session_id: str) -> int:
        """Delete a provider session's streams, records, and side files."""
        try:
            self.path.stat()
        except FileNotFoundError:
            return 0
        with self.connect(create=False) as db:
            db.execute(
                "DELETE FROM streams WHERE provider = ? "
                "AND (session_id = ? OR hook_session_id = ?)",
                (provider, session_id, session_id),
            )
            db.execute(
                "DELETE FROM side_files WHERE provider = ? AND session_id = ?",
                (provider, session_id),
            )
            return db.total_changes

    def stats(self) -> CaptureStats:
        """Count streams and payload bytes in a read-only snapshot."""
        try:
            self.path.stat()
        except FileNotFoundError:
            return {
                "streams": 0,
                "open": 0,
                "missing": 0,
                "raw_bytes": 0,
                "stored_bytes": 0,
                "skipped_lines": 0,
                "last_errors": [],
            }
        with self.connect(write=False) as db:
            row = db.execute(
                """
                SELECT count(*) AS streams,
                       count(*) FILTER (WHERE state = 'open') AS open,
                       count(*) FILTER (WHERE state = 'missing') AS missing,
                       coalesce(sum(raw_bytes), 0)
                         + (SELECT coalesce(sum(raw_bytes), 0) FROM side_files) AS raw_bytes,
                       coalesce(sum(skipped_lines), 0) AS skipped_lines,
                       (SELECT coalesce(sum(length(data)), 0) FROM chunks)
                         + (SELECT coalesce(sum(length(line)), 0) FROM tail)
                         + (SELECT coalesce(sum(length(data)), 0) FROM side_files) AS stored_bytes
                FROM streams
                """
            ).fetchone()
            last_errors = [
                item[0]
                for item in db.execute(
                    "SELECT last_error FROM streams WHERE last_error IS NOT NULL "
                    "ORDER BY last_read DESC, stream_pk DESC"
                )
            ]
        return {
            "streams": row["streams"],
            "open": row["open"],
            "missing": row["missing"],
            "raw_bytes": row["raw_bytes"],
            "stored_bytes": row["stored_bytes"],
            "skipped_lines": row["skipped_lines"],
            "last_errors": last_errors,
        }


def _initialize(db: sqlite3.Connection, *, create: bool) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 0 and create:
        # Recheck under the write lock so two first opens cannot initialize the same file.
        with db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version == 0 and not db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
    raise StorageError(
        f"Capture store version {version}, expected {SCHEMA_VERSION}. "
        "See docs/storage.md for version conversion (scripts/convert_capture_store.py)."
    )


def seal_tail(
    db: sqlite3.Connection,
    stream_pk: int,
    *,
    target_bytes: int = CHUNK_TARGET_BYTES,
    idle: bool = False,
) -> None:
    """Move one prefix of the tail into a chunk within the caller's write transaction."""
    if target_bytes <= 0:
        raise ValueError("Capture chunk target must be positive")
    raw = bytearray()
    first_record = last_record = record_count = 0
    with closing(
        db.execute(
            "SELECT record_seq, line FROM tail WHERE stream_pk = ? ORDER BY record_seq",
            (stream_pk,),
        )
    ) as rows:
        for record_seq, line in rows:
            if raw and len(raw) + len(line) + 1 > target_bytes:
                break
            if not raw:
                first_record = record_seq
            last_record = record_seq
            record_count += 1
            raw.extend(line)
            raw.extend(b"\n")
            if len(raw) >= target_bytes:
                break
        else:
            if not idle or not raw:
                return

    db.execute(
        """
        INSERT INTO chunks (
            stream_pk, chunk_seq, first_record, record_count, raw_bytes, codec, data
        )
        SELECT ?, coalesce(max(chunk_seq) + 1, 0), ?, ?, ?, 'zstd', ?
        FROM chunks WHERE stream_pk = ?
        """,
        (
            stream_pk,
            first_record,
            record_count,
            len(raw),
            zstd.compress(raw, level=ZSTD_LEVEL),
            stream_pk,
        ),
    )
    db.execute("DELETE FROM tail WHERE stream_pk = ? AND record_seq <= ?", (stream_pk, last_record))


def iter_lines(db: sqlite3.Connection, stream_pk: int) -> Iterator[bytes]:
    """Read a stream in capture order from a single transaction, with LF removed."""
    for chunk_seq, codec, data in db.execute(
        "SELECT chunk_seq, codec, data FROM chunks WHERE stream_pk = ? ORDER BY chunk_seq",
        (stream_pk,),
    ):
        if codec != "zstd":
            raise StorageError(f"Unsupported codec {codec!r} in capture chunk {chunk_seq}")
        with BytesIO(zstd.decompress(data)) as payload:
            for line in payload:
                yield line.removesuffix(b"\n")
    for (line,) in db.execute(
        "SELECT line FROM tail WHERE stream_pk = ? ORDER BY record_seq", (stream_pk,)
    ):
        yield line
