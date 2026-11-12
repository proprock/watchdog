"""SQLite storage for byte-exact transcript capture."""

import json
import socket
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from compression import zstd
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import NamedTuple, TypedDict
from uuid import UUID

from agent_watchdog import resources
from agent_watchdog.config import Limits, Project, project_aliases
from agent_watchdog.files import atomic_write, open_shared_read
from agent_watchdog.storage import EVENTS_SCHEMA_VERSION, QuotaExceeded, StorageError

CAPTURE_FILENAME = "capture.sqlite3"
SCHEMA_VERSION = 3
STORE_CONTRACT_VERSION = 1
STORE_MANIFEST_FILENAME = "store.json"
CHUNK_TARGET_BYTES = 4 * 1024**2
ZSTD_LEVEL = 12

CAPTURE_ENV_SCHEMA = (
    """
    CREATE TABLE blobs (
        digest TEXT PRIMARY KEY,
        raw_bytes INTEGER NOT NULL,
        codec TEXT NOT NULL,
        data BLOB NOT NULL
    )
    """,
    """
    CREATE TABLE session_env (
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        cwd TEXT,
        checkout_id TEXT,
        head TEXT,
        branch TEXT,
        harness TEXT NOT NULL,
        files TEXT NOT NULL,
        watchdog TEXT NOT NULL,
        initial_diff_digest TEXT,
        initial_diff_error TEXT,
        PRIMARY KEY (provider, session_id, captured_at)
    )
    """,
)

CAPTURE_V3_SCHEMA = (
    "ALTER TABLE session_env ADD COLUMN initial_files_changed INTEGER",
    "ALTER TABLE session_env ADD COLUMN initial_insertions INTEGER",
    "ALTER TABLE session_env ADD COLUMN initial_deletions INTEGER",
    "ALTER TABLE session_env ADD COLUMN initial_untracked INTEGER",
    """
    CREATE TABLE checkout_states (
        checkout_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        trigger TEXT NOT NULL,
        base_head TEXT,
        head TEXT,
        branch TEXT,
        files_changed INTEGER,
        insertions INTEGER,
        deletions INTEGER,
        untracked INTEGER,
        diff_digest TEXT,
        error TEXT,
        PRIMARY KEY (checkout_id, session_id, observed_at)
    )
    """,
    "CREATE INDEX checkout_states_observed ON checkout_states (checkout_id, observed_at)",
)

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
    *CAPTURE_ENV_SCHEMA,
    *CAPTURE_V3_SCHEMA,
)


class CaptureStats(TypedDict):
    streams: int
    open: int
    missing: int
    raw_bytes: int
    stored_bytes: int
    skipped_lines: int
    last_errors: list[str]


class CheckoutStats(NamedTuple):
    files_changed: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    untracked: int | None = None


@dataclass(frozen=True, slots=True)
class CheckoutOutcome:
    checkout_id: str
    observed_at: datetime
    provider: str
    session_id: str
    trigger: str
    base_head: str | None
    head: str | None
    branch: str | None
    stats: CheckoutStats
    diff_digest: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SessionEnvironment:
    provider: str
    session_id: str
    captured_at: str
    cwd: str | None
    checkout_id: str | None
    head: str | None
    branch: str | None
    harness: Mapping[str, object]
    files: list[dict[str, str | None]]
    watchdog: Mapping[str, object]
    initial_diff_digest: str | None = None
    initial_diff_error: str | None = None
    initial_stats: CheckoutStats = CheckoutStats()


def _checkout_due(
    db: sqlite3.Connection, checkout_id: str, now: datetime, head: str | None
) -> bool:
    previous = db.execute(
        "SELECT head FROM checkout_states WHERE checkout_id = ? AND observed_at > ? "
        "ORDER BY observed_at DESC LIMIT 1",
        (checkout_id, (now - timedelta(seconds=30)).isoformat()),
    ).fetchone()
    return previous is None or (
        head is not None and previous["head"] is not None and head != previous["head"]
    )


def _store_blob(db: sqlite3.Connection, digest: str, raw: bytes, remaining: int) -> int | None:
    if db.execute("SELECT 1 FROM blobs WHERE digest = ?", (digest,)).fetchone() is not None:
        return remaining
    data = zstd.compress(raw, level=1)
    needed = 8192 + 4 * len(data)
    if needed > remaining:
        return None
    db.execute("INSERT INTO blobs VALUES (?, ?, 'zstd', ?)", (digest, len(raw), data))
    return remaining - needed


def write_manifest(
    project_dir: Path,
    *,
    machine_id: UUID,
    project: Project,
    events_schema: int = EVENTS_SCHEMA_VERSION,
    name: str | None = None,
) -> None:
    """Publish store metadata when its content changes."""
    path = project_dir / STORE_MANIFEST_FILENAME
    manifest = {
        "contract": STORE_CONTRACT_VERSION,
        "machine_id": str(machine_id),
        "hostname": socket.gethostname(),
        "os": sys.platform,
        "project_id": str(project.id),
        "root": str(project.root),
        "git_common_dir": str(project.git_common_dir) if project.git_common_dir else None,
        "name": name if name is not None else project_aliases((project,))[project.id],
        "events_schema": events_schema,
        "capture_schema": SCHEMA_VERSION,
    }
    try:
        with open_shared_read(path) as stream:
            previous = json.load(stream)
    except FileNotFoundError, ValueError:
        previous = None
    if isinstance(previous, dict):
        written_at = previous.pop("written_at", None)
        if isinstance(written_at, str) and previous == manifest:
            return
    content = json.dumps({**manifest, "written_at": datetime.now(UTC).isoformat()})
    atomic_write(path, (content + "\n").encode("utf-8"), allow_open_readers=True)


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

    def has_session_env(self, provider: str, session_id: str) -> bool:
        if not self.path.is_file():
            return False
        with self.connect(write=False) as db:
            return (
                db.execute(
                    "SELECT 1 FROM session_env WHERE provider = ? AND session_id = ? LIMIT 1",
                    (provider, session_id),
                ).fetchone()
                is not None
            )

    def record_session_env(
        self,
        snapshot: SessionEnvironment,
        blobs: Mapping[str, bytes],
        *,
        capture_content: bool,
        limits: Limits | None = None,
    ) -> bool:
        """Persist one changed environment and any new content in one transaction."""
        key = (snapshot.provider, snapshot.session_id)
        fields = (
            snapshot.cwd,
            snapshot.checkout_id,
            snapshot.head,
            snapshot.branch,
            json.dumps(dict(snapshot.harness), sort_keys=True),
            json.dumps(snapshot.files, sort_keys=True),
            json.dumps(dict(snapshot.watchdog), sort_keys=True),
        )
        root = self.path.parent
        with resources.admission(root), ExitStack() as stack:
            # Allow for SQLite pages and the write-ahead log, as transcript capture does.
            metadata_bytes = sum(len(value.encode()) for value in (*key, *fields) if value)
            remaining = resources.available(root, limits or Limits()) - (
                64 * 1024 + 4 * metadata_bytes
            )
            if remaining < 0:
                raise QuotaExceeded("Session environment exceeds capture headroom")
            db = stack.enter_context(self.connect())
            previous = db.execute(
                "SELECT cwd, checkout_id, head, branch, harness, files, watchdog "
                "FROM session_env WHERE provider = ? AND session_id = ? "
                "ORDER BY captured_at DESC LIMIT 1",
                key,
            ).fetchone()
            if previous is not None and tuple(previous) == fields:
                return False
            initial_digest = snapshot.initial_diff_digest if previous is None else None
            initial_error = snapshot.initial_diff_error if previous is None else None
            initial_stats = snapshot.initial_stats if previous is None else CheckoutStats()
            omitted: set[str] = set()
            if capture_content:
                digests = {item["digest"] for item in snapshot.files}
                digests.add(initial_digest)
                for digest, raw in blobs.items():
                    if digest not in digests:
                        continue
                    left = _store_blob(db, digest, raw, remaining)
                    if left is None:
                        omitted.add(digest)
                        continue
                    remaining = left
            if omitted:
                files = [
                    item | {"error": "quota"} if item["digest"] in omitted else item
                    for item in snapshot.files
                ]
                fields = (*fields[:5], json.dumps(files, sort_keys=True), fields[6])
                if initial_digest in omitted:
                    initial_error = "quota"
                if previous is not None and tuple(previous) == fields:
                    return False
            db.execute(
                "INSERT INTO session_env VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    snapshot.captured_at,
                    *fields,
                    initial_digest,
                    initial_error,
                    *initial_stats,
                ),
            )
            return True

    def checkout_outcome_base(
        self, checkout_id: str, provider: str, session_id: str, now: datetime, head: str | None
    ) -> tuple[bool, str | None]:
        """Read the debounce state and the session's first observed commit."""
        if not self.path.is_file():
            return True, None
        with self.connect(write=False) as db:
            if not _checkout_due(db, checkout_id, now, head):
                return False, None
            first = db.execute(
                "SELECT checkout_id, head FROM session_env WHERE provider = ? AND session_id = ? "
                "ORDER BY captured_at LIMIT 1",
                (provider, session_id),
            ).fetchone()
            return True, first["head"] if first is not None and first[
                "checkout_id"
            ] == checkout_id else None

    def record_checkout_outcome(
        self, snapshot: CheckoutOutcome, blobs: Mapping[str, bytes], *, limits: Limits
    ) -> None:
        root = self.path.parent
        with resources.admission(root), self.connect() as db:
            if not _checkout_due(db, snapshot.checkout_id, snapshot.observed_at, snapshot.head):
                return
            remaining = resources.available(root, limits) - 64 * 1024
            if remaining < 0:
                raise QuotaExceeded("Checkout outcome exceeds capture headroom")
            digest, error = snapshot.diff_digest, snapshot.error
            if digest is not None and _store_blob(db, digest, blobs[digest], remaining) is None:
                digest, error = None, "quota"
            db.execute(
                "INSERT INTO checkout_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.checkout_id,
                    snapshot.observed_at.isoformat(),
                    snapshot.provider,
                    snapshot.session_id,
                    snapshot.trigger,
                    snapshot.base_head,
                    snapshot.head,
                    snapshot.branch,
                    *snapshot.stats,
                    digest,
                    error,
                ),
            )

    def purge(self, provider: str, session_id: str) -> int:
        """Delete a provider session and its unshared capture content."""
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
            removed = db.execute(
                "DELETE FROM session_env WHERE provider = ? AND session_id = ? "
                "RETURNING initial_diff_digest, files",
                (provider, session_id),
            ).fetchall()
            digests = {row[0] for row in removed if row[0] is not None}
            digests.update(
                item["digest"] for row in removed for item in json.loads(row[1]) if item["digest"]
            )
            digests.update(
                row[0]
                for row in db.execute(
                    "DELETE FROM checkout_states WHERE provider = ? AND session_id = ? "
                    "RETURNING diff_digest",
                    (provider, session_id),
                )
                if row[0] is not None
            )
            if digests:
                db.execute(
                    """
                    DELETE FROM blobs WHERE digest IN (SELECT value FROM json_each(?))
                    AND NOT EXISTS (
                        SELECT 1 FROM session_env AS env
                        WHERE env.initial_diff_digest = blobs.digest OR EXISTS (
                            SELECT 1 FROM json_each(env.files) AS file
                            WHERE file.value ->> 'digest' = blobs.digest
                        )
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM checkout_states AS outcome
                        WHERE outcome.diff_digest = blobs.digest
                    )
                    """,
                    (json.dumps(sorted(digests)),),
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
                         + (SELECT coalesce(sum(raw_bytes), 0) FROM side_files)
                         + (SELECT coalesce(sum(raw_bytes), 0) FROM blobs) AS raw_bytes,
                       coalesce(sum(skipped_lines), 0) AS skipped_lines,
                       (SELECT coalesce(sum(length(data)), 0) FROM chunks)
                         + (SELECT coalesce(sum(length(line)), 0) FROM tail)
                         + (SELECT coalesce(sum(length(data)), 0) FROM side_files)
                         + (SELECT coalesce(sum(length(data)), 0) FROM blobs) AS stored_bytes
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
        "See docs/storage.md for version conversion (scripts/capture_schema_v3.py)."
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
