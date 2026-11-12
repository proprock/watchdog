"""One SQLite writer per project. No daemon or hook installation."""

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from agent_watchdog import analysis, codex_attribution, facts, resources, state
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.files import atomic_write as atomic_write
from agent_watchdog.state import SessionState, StateKey, fold, initial, key_of
from agent_watchdog.state import apply as apply_state

EVENTS_SCHEMA_VERSION = 9

# Sessions whose events or analysis version outran their materialized findings.
PENDING_SESSIONS = (
    "SELECT e.provider, e.session_id FROM events e WHERE e.session_id IS NOT NULL "
    "GROUP BY e.provider, e.session_id "
    "HAVING MAX(e.rowid) > COALESCE((SELECT a.last_event_rowid FROM session_analysis a "
    "WHERE a.provider = e.provider AND a.session_id = e.session_id AND a.rule_version = ?), 0)"
)
# Tables keyed by (provider, session_id) that follow the session's events.
SESSION_TABLES = ("session_state", "session_findings", "session_analysis")

# Manual annotation vocabularies (schema v7, WD-012 calibration).
PROGRESS_STATES = ("progress", "slow", "stuck", "externally_blocked")
FINDING_VERDICTS = ("true_positive", "false_positive", "uncertain")
# A transcript reader's stored ``tail``: one unfinished line of up to 32 MiB
# (``transcripts.MAX_LINE_BYTES``) after a held Claude response of up to 512 KiB.
TRANSCRIPT_TAIL_BYTES = 33 * 1024**2
SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1
SQLITE_UNSIGNED_INTEGER_MAX = 2**64 - 1
CODEX_STATE_PROJECTION_VERSION = "wd-164.v1|wd-165.v1"


class StorageError(ValueError):
    """Unsupported storage or an invalid storage operation."""


class WriterBusy(StorageError):
    """Another writer holds the project lock."""


class RejectedEvent(StorageError):
    """Input cannot be accepted; quarantine it without stopping the queue."""


class QuotaExceeded(StorageError):
    """No room for another write; retained input may be retried after cleanup."""


class MaintenancePending(StorageError):
    """Cleanup yielded at its deadline; retry input without recording quota loss."""


@dataclass
class MaintenanceResult:
    pending: bool
    exhausted: bool = False
    space_available: bool = False


@dataclass
class MaintenanceBudget:
    """One tick's maintenance allowance shared across project writers."""

    remaining: float = 0.2


@dataclass
class MaintenanceState:
    """Ephemeral progress carried by the daemon between project Store opens."""

    phase: str = "start"
    now: datetime | None = None
    cursor: int = 0
    cursor_time: int = SQLITE_INTEGER_MIN
    ceiling: int = 0
    directory: int = 0
    files: Iterator[Path] | None = None
    usage: int = 0
    prune_table: int = 0
    after_reclaim: str = "space"
    after_account: str = "pressure_content"
    session: str | None = None
    session_selected: bool = False
    oldest: int | None = None


MAINTENANCE_CHUNK = 32
MAINTENANCE_CONTENT_BYTES = 1024**2
_UNPINNED = "(events.session_id IS NULL OR events.session_id NOT IN (SELECT session_id FROM pins))"
_HAS_CONTENT = (
    "NOT (json_extract(envelope, '$.availability.content') IS 'unavailable' "
    "AND json_extract(envelope, '$.payload') = '{}')"
)


def _maintenance_files(directory: Path, *, recursive: bool = False) -> Iterator[Path]:
    """Keep a scandir cursor instead of materializing a large directory listing."""
    if directory.is_symlink() or directory.is_junction():
        raise StorageError("Linked data directory")
    if not directory.exists():
        return
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if recursive and entry.is_dir(follow_symlinks=False) and not path.is_junction():
                yield from _maintenance_files(path, recursive=True)
            else:
                yield path


def sqlite_integer(value: int) -> int:
    """Encode an unsigned filesystem identifier in SQLite's signed INTEGER range."""
    if SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
        return value
    if 0 <= value <= SQLITE_UNSIGNED_INTEGER_MAX:
        return value - (SQLITE_UNSIGNED_INTEGER_MAX + 1)
    raise StorageError("Integer is outside SQLite range")


def _check_verdict(rule: str, rule_version: str, fingerprint: str, verdict: str) -> None:
    if not rule.strip() or not rule_version.strip():
        raise StorageError("A rule and rule version are required")
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise StorageError("An evidence fingerprint must be 64 lowercase hexadecimal characters")
    if verdict not in FINDING_VERDICTS:
        raise StorageError("Invalid finding verdict")


def _check_note(note: str | None) -> str | None:
    if note is None:
        return None
    note = note.strip()
    if not note or len(note) > 2000:
        raise StorageError("Reviewer note must contain 1..2000 characters")
    return note


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    """Keep the lock file in place; closing its handle releases ownership."""
    # Do not retry a failed initialization write implicitly when closing a buffer.
    try:
        stream = path.open("a+b", buffering=0)
    except OSError as error:
        # A concurrent creator (another contender, antivirus scanning the new
        # file) can win a transient sharing violation at open time; treat it
        # the same as losing the byte-range lock so control_lock's retry loop
        # covers it too, instead of surfacing a raw, unretried OSError.
        raise WriterBusy("Project writer is unavailable") from error
    with stream:
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
    document = event.model_dump(mode="json")
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
        maintenance_state: MaintenanceState | None = None,
    ):
        if artifact_bytes <= 0:
            raise StorageError("Artifact limit must be positive")
        self.root = root.resolve()
        self.project_id = project_id
        self.artifact_bytes = artifact_bytes
        self.limits = limits or Limits()
        self._connection: sqlite3.Connection | None = None
        self._stack = ExitStack()
        # Session states written since this store opened; also the read-through cache.
        self.touched: dict[StateKey, SessionState] = {}
        self._codex_state_deferred = False
        self._batch_remaining: int | None = None
        self.maintenance_state = maintenance_state or MaintenanceState()
        self.maintenance_deadline: float | None = None
        self.maintenance_budget: MaintenanceBudget | None = None

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
                self._flush_codex_state()
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
        nested = db.in_transaction
        db.execute("SAVEPOINT event_write" if nested else "BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("RELEASE event_write" if nested else "COMMIT")
        except BaseException:
            if db.in_transaction:
                if nested:
                    db.execute("ROLLBACK TO event_write")
                    db.execute("RELEASE event_write")
                else:
                    db.execute("ROLLBACK")
            raise

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Commit one bounded reader window, including its state projection.

        The admission lock excludes publishers while conservative per-event
        reservations account for pages not yet visible in the WAL. Quota pressure
        aborts this window; the caller can retry with ordinary per-event cleanup.
        """
        if self.connection.in_transaction or self._batch_remaining is not None:
            raise StorageError("A writer batch is already active")
        touched = self.touched.copy()
        with resources.admission(self.root):
            self._batch_remaining = resources.available(self.root, self.limits)
            try:
                with self._transaction(), self.defer_codex_state():
                    yield
            except BaseException:
                self.touched = touched
                raise
            finally:
                self._batch_remaining = None

    def _initialize(self) -> None:
        db = self.connection
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= EVENTS_SCHEMA_VERSION:
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
        if version < 6:
            size = (self.root / "events.sqlite3").stat().st_size
            if resources.available(self.root, self.limits) < max(65536, size // 2):
                raise QuotaExceeded("Insufficient space for the v6 telemetry migration")
            with self._transaction():
                columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
                if "provider" not in columns:
                    db.execute("ALTER TABLE events ADD COLUMN provider TEXT")
                db.execute("DROP TABLE IF EXISTS event_facts")
                db.execute(facts.CREATE_TABLE)
                trackers: dict[tuple[str | None, str | None], facts.ConfigTracker] = {}
                agent_turns: dict[tuple[str, str], str] = {}
                for rowid, event_id, kind, received_at, document in db.execute(
                    "SELECT rowid, event_id, kind, received_at, envelope FROM events ORDER BY rowid"
                ).fetchall():
                    envelope = json.loads(document)
                    session_id = envelope.get("session_id")
                    agent_id = envelope.get("agent_id")
                    tracker = trackers.setdefault(
                        (envelope.get("provider"), agent_id), facts.ConfigTracker()
                    )
                    turn_id, _ = facts.turn_of(envelope)
                    inherited = tracker.resolve(session_id, turn_id)
                    parent_turn = (
                        agent_turns.get((session_id, agent_id))
                        if kind == "usage"
                        and isinstance(session_id, str)
                        and isinstance(agent_id, str)
                        and envelope.get("provider") == "claude"
                        else None
                    )
                    row = facts.project_event(
                        event_id,
                        kind,
                        received_at,
                        envelope,
                        inherited=inherited,
                        parent_turn=parent_turn,
                    )
                    db.execute(
                        "UPDATE events SET provider=? WHERE rowid=?", (row["provider"], rowid)
                    )
                    db.execute(facts.INSERT, [row[name] for name in facts.FACT_COLUMNS])
                    if (
                        row["provider"] == "claude"
                        and kind in ("agent.start", "agent.end")
                        and isinstance(session_id, str)
                        and isinstance(agent_id, str)
                        and row["turn_id"]
                    ):
                        agent_turns[(session_id, agent_id)] = row["turn_id"]
                    tracker.observe(session_id, row["turn_id"], *facts.observation(row))
                for statement in facts.CREATE_INDEXES:
                    db.execute(statement)
                db.execute("PRAGMA user_version=6")
        if version < 7:
            with self._transaction():
                columns = {row[1] for row in db.execute("PRAGMA table_info(session_labels)")}
                if "progress_state" not in columns:
                    db.execute("ALTER TABLE session_labels ADD COLUMN progress_state TEXT")
                if "reviewer_note" not in columns:
                    db.execute("ALTER TABLE session_labels ADD COLUMN reviewer_note TEXT")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS finding_verdicts ("
                    "provider TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "rule TEXT NOT NULL, rule_version TEXT NOT NULL, "
                    "evidence_fingerprint TEXT NOT NULL, verdict TEXT NOT NULL, "
                    "note TEXT, reviewed_at TEXT NOT NULL, "
                    "PRIMARY KEY(provider, session_id, rule, rule_version, evidence_fingerprint))"
                )
                # A checkout is shared by both providers, so a diff finding has no provider.
                db.execute(
                    "CREATE TABLE IF NOT EXISTS checkout_finding_verdicts ("
                    "checkout_id TEXT NOT NULL, rule TEXT NOT NULL, rule_version TEXT NOT NULL, "
                    "evidence_fingerprint TEXT NOT NULL, verdict TEXT NOT NULL, "
                    "note TEXT, reviewed_at TEXT NOT NULL, "
                    "PRIMARY KEY(checkout_id, rule, rule_version, evidence_fingerprint))"
                )
                db.execute("PRAGMA user_version=7")
        if version < 8:
            size = (self.root / "events.sqlite3").stat().st_size
            if resources.available(self.root, self.limits) < max(65536, size // 8):
                raise QuotaExceeded("Insufficient space for the v8 session-state migration")
            with self._transaction():
                self._migrate_v8()
                db.execute("PRAGMA user_version=8")
        if version < 9:
            with self._transaction():
                columns = {row[1] for row in db.execute("PRAGMA table_info(session_analysis)")}
                if "rule_version" not in columns:
                    db.execute("ALTER TABLE session_analysis ADD COLUMN rule_version TEXT")
                db.execute("PRAGMA user_version=9")
        # Additive reporting indexes also apply to already-current stores.
        # The implicit rowid covers pending-session watermarks and first-copy usage.
        db.execute(
            "CREATE INDEX IF NOT EXISTS events_provider_session "
            "ON events(provider,session_id) WHERE session_id IS NOT NULL"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS event_facts_usage_identity "
            "ON event_facts(provider,session_id,agent_id,native_event_id) "
            "WHERE kind='usage' AND native_event_id IS NOT NULL"
        )
        self._refresh_codex_attribution()
        self._refresh_state_projection(invalidate_findings=version >= 8)
        # Existing v1 stores need a one-time rebuild to support physical reclamation.
        if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            size = (self.root / "events.sqlite3").stat().st_size
            if resources.available(self.root, self.limits) < 2 * size:
                raise QuotaExceeded("Insufficient migration space")
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("VACUUM")
        db.execute(
            "CREATE INDEX IF NOT EXISTS event_facts_retention "
            "ON event_facts(received_at_us,event_id)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS artifacts_digest ON artifacts(digest)")

    def _refresh_codex_attribution(self) -> None:
        """Rebuild this narrow facts projection once, without rewriting raw events."""
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS fact_projection_versions "
                "(name TEXT PRIMARY KEY, version TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS codex_attribution_dirty "
                "(session_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
                "PRIMARY KEY(session_id,agent_id))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS event_facts_native_config "
                "ON event_facts(provider,session_id,agent_id,"
                "COALESCE(occurred_at_us,received_at_us))"
            )
            row = db.execute(
                "SELECT version FROM fact_projection_versions WHERE name='codex-attribution'"
            ).fetchone()
            if row != (codex_attribution.VERSION,):
                codex_attribution.refresh(db)
                db.execute(
                    "INSERT OR REPLACE INTO fact_projection_versions VALUES (?,?)",
                    ("codex-attribution", codex_attribution.VERSION),
                )

    def _refresh_state_projection(self, *, invalidate_findings: bool) -> None:
        """Refold changed reducers atomically before restoring the daemon's state."""
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS state_projection_versions ("
                "provider TEXT PRIMARY KEY, version TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS codex_state_dirty ("
                "session_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
                "PRIMARY KEY(session_id, agent_id))"
            )
            for provider in ("codex", "claude"):
                version = state.VERSION
                if provider == "codex":
                    version += "|" + CODEX_STATE_PROJECTION_VERSION
                row = db.execute(
                    "SELECT version FROM state_projection_versions WHERE provider=?", (provider,)
                ).fetchone()
                if row == (version,):
                    continue
                size = (self.root / "events.sqlite3").stat().st_size
                if resources.available(self.root, self.limits) < max(65536, size // 8):
                    raise QuotaExceeded("Insufficient space for the state projection refresh")
                db.execute("DELETE FROM session_state WHERE provider=?", (provider,))
                sessions = db.execute(
                    "SELECT DISTINCT session_id FROM events "
                    "WHERE provider=? AND session_id IS NOT NULL",
                    (provider,),
                ).fetchall()
                for (session_id,) in sessions:
                    documents = (
                        json.loads(row[0])
                        for row in db.execute(
                            "SELECT envelope FROM events WHERE provider=? AND session_id=? "
                            "ORDER BY received_at, rowid",
                            (provider, session_id),
                        )
                    )
                    for rebuilt in fold(documents).values():
                        self._write_state(db, rebuilt)
                if invalidate_findings:
                    db.execute("DELETE FROM session_analysis WHERE provider=?", (provider,))
                db.execute(
                    "INSERT OR REPLACE INTO state_projection_versions VALUES (?, ?)",
                    (provider, version),
                )

    @staticmethod
    def _fingerprint(document: str, references: Mapping[str, str]) -> str:
        # Delivery telemetry is intentionally mutable: a replay can reach the
        # spool/SQLite stages at a different instant without changing the
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

    def usage_native_ids(
        self, provider: str, session_id: str, agent_id: str | None = None
    ) -> set[str]:
        """Native ids of the usage events already stored for one conversation agent.

        Lets a reader recognise a response stored under any earlier event id,
        e.g. one derived from another spelling of the same transcript path or
        computed by an earlier reader version, and skip it instead of storing it
        again or conflicting with its receipt.
        """
        rows = self.connection.execute(
            "SELECT native_event_id FROM event_facts WHERE provider=? AND session_id=? "
            "AND agent_id IS ? AND kind='usage' AND native_event_id IS NOT NULL",
            (provider, session_id, agent_id),
        )
        return {row[0] for row in rows}

    def put(self, event: Envelope, *, artifacts: Mapping[str, bytes] | None = None) -> bool:
        with nullcontext() if self._batch_remaining is not None else resources.admission(self.root):
            return self._put(event, artifacts=artifacts)

    def _put(self, event: Envelope, *, artifacts: Mapping[str, bytes] | None = None) -> bool:
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
            for data in artifacts.values():
                data.decode("utf-8")
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
        if self._batch_remaining is not None:
            if self._batch_remaining < needed:
                raise QuotaExceeded("Writer batch reservation exhausted")
        else:
            maintenance = None
            if resources.available(self.root, self.limits) < needed:
                maintenance = self._maintain(datetime.now(UTC), needed)
                if maintenance.pending and not maintenance.space_available:
                    raise MaintenancePending("Project cleanup deadline reached")
            if resources.available(self.root, self.limits) < needed:
                if maintenance is not None and not maintenance.exhausted:
                    raise MaintenancePending("Project storage changed during cleanup")
                raise QuotaExceeded("Project quota or disk reserve reached")
        applied: SessionState | None = None
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
            cursor = db.execute(
                "INSERT INTO events (event_id, received_at, session_id, kind, envelope, provider) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event.received_at.isoformat(),
                    event.session_id,
                    event.kind,
                    document,
                    event.provider,
                ),
            )
            db.executemany(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                [(event_id, name, references[name], len(data)) for name, data in artifacts.items()],
            )
            db.execute("INSERT INTO receipts VALUES (?, ?)", (event_id, fingerprint))
            envelope = json.loads(document)
            self._project_fact(db, event_id, event.kind, envelope, cursor.lastrowid)
            applied = self._apply_state(db, envelope)
            self._register_transcript_source(db, event)
        if applied is not None:
            self.touched[applied.provider, applied.session_id, applied.agent_id] = applied
        if self._batch_remaining is not None:
            self._batch_remaining -= needed
        return True

    @contextmanager
    def defer_codex_state(self) -> Iterator[None]:
        """Replay each affected Codex identity once after a reader window.

        Raw observations and dirty identities commit together. An interrupted
        window or failed replay keeps its recovery marker for the next open.
        """
        if self._codex_state_deferred:
            raise StorageError("Codex state is already deferred")
        self._codex_state_deferred = True
        try:
            yield
        except BaseException:
            raise
        else:
            with (
                nullcontext()
                if self._batch_remaining is not None
                else resources.admission(self.root)
            ):
                self._flush_codex_state()
        finally:
            self._codex_state_deferred = False

    def _flush_codex_state(self) -> None:
        self._flush_codex_attribution()
        keys = self.connection.execute(
            "SELECT session_id, agent_id FROM codex_state_dirty ORDER BY session_id, agent_id"
        ).fetchall()
        for session_id, agent_id in keys:
            key = ("codex", session_id, agent_id)
            with self._transaction() as db:
                updated = self._replay_codex_state(db, key)
                self._write_state(db, updated)
                db.execute(
                    "DELETE FROM codex_state_dirty WHERE session_id=? AND agent_id=?", key[1:]
                )
            self.touched[key] = updated

    def _flush_codex_attribution(self) -> None:
        identities = self.connection.execute(
            "SELECT session_id,agent_id FROM codex_attribution_dirty"
        ).fetchall()
        for session_id, agent_id in identities:
            with self._transaction() as db:
                codex_attribution.refresh(db, identity=(session_id, agent_id or None))
                db.execute(
                    "DELETE FROM codex_attribution_dirty WHERE session_id=? AND agent_id=?",
                    (session_id, agent_id),
                )

    @staticmethod
    def _replay_codex_state(db: sqlite3.Connection, key: StateKey) -> SessionState:
        # Rollout delivery may enrich an earlier hook completion. Replay it at
        # that completion's position, preserving newer turns and edit order.
        documents = (
            json.loads(row[0])
            for row in db.execute(
                "SELECT envelope FROM events WHERE provider=? AND session_id=? "
                "ORDER BY received_at, rowid",
                key[:2],
            )
        )
        return fold(item for item in documents if key_of(item) == key).get(key) or initial(*key)

    def _apply_state(self, db: sqlite3.Connection, envelope: dict) -> SessionState | None:
        """Fold the event into its session state row, atomically with the event itself."""
        key = key_of(envelope)
        if key is None:
            return None
        dirty = (
            key[0] == "codex"
            and db.execute(
                "SELECT 1 FROM codex_state_dirty WHERE session_id=? AND agent_id=?", key[1:]
            ).fetchone()
        )
        if key[0] == "codex" and (envelope.get("kind") in ("tool.start", "tool.finish") or dirty):
            if self._codex_state_deferred:
                db.execute("INSERT OR IGNORE INTO codex_state_dirty VALUES (?, ?)", key[1:])
                return None
            updated = self._replay_codex_state(db, key)
            db.execute("DELETE FROM codex_state_dirty WHERE session_id=? AND agent_id=?", key[1:])
        else:
            current = self.touched.get(key) or self._load_state(db, key) or initial(*key)
            updated = apply_state(current, envelope)
        self._write_state(db, updated)
        return updated

    @staticmethod
    def _load_state(db: sqlite3.Connection, key: StateKey) -> SessionState | None:
        row = db.execute(
            "SELECT state_json FROM session_state WHERE provider=? AND session_id=? AND agent_id=?",
            key,
        ).fetchone()
        return SessionState.from_json(row[0]) if row else None

    @staticmethod
    def _write_state(db: sqlite3.Connection, state: SessionState) -> None:
        db.execute(
            "INSERT INTO session_state (provider, session_id, agent_id, state_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, session_id, agent_id) "
            "DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (
                state.provider,
                state.session_id,
                state.agent_id,
                state.to_json(),
                datetime.now(UTC).isoformat(),
            ),
        )

    def session_states(self) -> dict[StateKey, SessionState]:
        """Every stored session state, for the daemon to restore after a restart."""
        rows = self.connection.execute("SELECT state_json FROM session_state").fetchall()
        states = (SessionState.from_json(row[0]) for row in rows)
        return {(item.provider, item.session_id, item.agent_id): item for item in states}

    def pending_sessions(self) -> list[tuple[str, str]]:
        """Sessions with newer events or an obsolete analysis version."""
        return [
            (row[0], row[1])
            for row in self.connection.execute(PENDING_SESSIONS, (analysis.RULE_VERSION,))
        ]

    def refresh_session_findings(
        self,
        provider: str,
        session_id: str,
        *,
        now: datetime | None = None,
        snapshot_cache: dict[frozenset[str], list[dict]] | None = None,
    ) -> None:
        """Re-run ``analyze`` over one session and replace its materialized findings.

        Pass one ``snapshot_cache`` for a batch of sessions of an unchanged store: the
        checkout's diff-snapshot attribution is the costly part and is the same for all.
        """
        computed = self._analyze_session(provider, session_id, snapshot_cache)
        with resources.admission(self.root), self._transaction() as db:
            self._store_findings(db, provider, session_id, computed, now or datetime.now(UTC))

    def _analyze_session(
        self,
        provider: str,
        session_id: str,
        snapshot_cache: dict[frozenset[str], list[dict]] | None = None,
    ) -> tuple[dict, int] | None:
        # Lazy: inspection imports this module for its error and envelope types.
        from agent_watchdog import inspection
        from agent_watchdog.analysis import analyze

        db = self.connection
        documents, snapshots = inspection.analysis_inputs(db, provider, session_id, snapshot_cache)
        if not documents:
            return None
        last_rowid = db.execute(
            "SELECT MAX(rowid) FROM events WHERE provider=? AND session_id=?",
            (provider, session_id),
        ).fetchone()[0]
        report = analyze((persisted_envelope(item) for item in documents), snapshots=snapshots)
        return report, last_rowid

    @staticmethod
    def _store_findings(
        db: sqlite3.Connection,
        provider: str,
        session_id: str,
        computed: tuple[dict, int] | None,
        now: datetime,
    ) -> None:
        for table in ("session_findings", "session_analysis"):
            db.execute(
                f"DELETE FROM {table} WHERE provider=? AND session_id=?", (provider, session_id)
            )
        if computed is None:
            return
        report, last_rowid = computed
        stamp = now.isoformat()
        db.execute(
            "INSERT INTO session_analysis "
            "(provider, session_id, computed_at, last_event_rowid, gaps_json, rule_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                provider,
                session_id,
                stamp,
                last_rowid,
                json.dumps(report["gaps"]),
                report["rule_version"],
            ),
        )
        db.executemany(
            "INSERT OR REPLACE INTO session_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    provider,
                    session_id,
                    finding["rule"],
                    finding["rule_version"],
                    finding["fingerprint"],
                    finding["count"],
                    json.dumps(finding["evidence_ids"]),
                    stamp,
                )
                for finding in report["findings"]
            ],
        )

    def _migrate_v8(self) -> None:
        """Create the session tables and replay every stored event into them."""
        db = self.connection
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_state ("
            "provider TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
            "state_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(provider, session_id, agent_id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_findings ("
            "provider TEXT NOT NULL, session_id TEXT NOT NULL, rule TEXT NOT NULL, "
            "rule_version TEXT NOT NULL, fingerprint TEXT NOT NULL, count INTEGER NOT NULL, "
            "evidence_json TEXT NOT NULL, computed_at TEXT NOT NULL, "
            "PRIMARY KEY(provider, session_id, rule, rule_version, fingerprint))"
        )
        # Also the staleness watermark: findings are current while last_event_rowid
        # equals the session's newest event.
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_analysis ("
            "provider TEXT NOT NULL, session_id TEXT NOT NULL, computed_at TEXT NOT NULL, "
            "last_event_rowid INTEGER NOT NULL, gaps_json TEXT NOT NULL, rule_version TEXT, "
            "PRIMARY KEY(provider, session_id))"
        )
        for table in SESSION_TABLES:
            db.execute(f"DELETE FROM {table}")
        envelopes = (
            json.loads(row[0]) for row in db.execute("SELECT envelope FROM events ORDER BY rowid")
        )
        for rebuilt in fold(envelopes).values():
            self._write_state(db, rebuilt)
        now = datetime.now(UTC)
        snapshot_cache: dict[frozenset[str], list[dict]] = {}
        for provider, session_id in db.execute(
            "SELECT DISTINCT provider, session_id FROM events WHERE session_id IS NOT NULL"
        ).fetchall():
            try:
                computed = self._analyze_session(provider, session_id, snapshot_cache)
                self._store_findings(db, provider, session_id, computed, now)
            except ValueError:
                # An unreadable envelope must not block the upgrade; the session stays pending.
                continue

    def _project_fact(
        self,
        db: sqlite3.Connection,
        event_id: str,
        kind: str,
        envelope: dict,
        rowid: int | None,
    ) -> None:
        """Materialize the v6 ``event_facts`` row for a freshly inserted event."""
        inherited: tuple[str | None, str | None] = (None, None)
        parent_turn: str | None = None
        if kind == "usage" and rowid is not None:
            session_id = envelope.get("session_id")
            agent_id = envelope.get("agent_id")
            turn_id, _ = facts.turn_of(envelope)
            inherited = (
                codex_attribution.resolve(db, envelope)
                if envelope.get("provider") == "codex"
                else self._inherited_config(
                    db,
                    session_id,
                    agent_id if isinstance(agent_id, str) else None,
                    turn_id,
                    rowid,
                    provider=envelope.get("provider"),
                )
            )
            if isinstance(agent_id, str) and agent_id and envelope.get("provider") == "claude":
                parent_turn = self._parent_turn(db, session_id, agent_id, rowid)
        row = facts.project_event(
            event_id,
            kind,
            envelope.get("received_at", ""),
            envelope,
            inherited=inherited,
            parent_turn=parent_turn,
        )
        db.execute(facts.INSERT, [row[name] for name in facts.FACT_COLUMNS])
        if codex_attribution.is_context(envelope):
            if self._codex_state_deferred:
                db.execute(
                    "INSERT OR IGNORE INTO codex_attribution_dirty VALUES (?,?)",
                    (envelope.get("session_id"), envelope.get("agent_id") or ""),
                )
            else:
                codex_attribution.refresh(db, envelope)

    @staticmethod
    def _inherited_config(
        db: sqlite3.Connection,
        session_id: object,
        agent_id: str | None,
        turn_id: str | None,
        before_rowid: int,
        *,
        provider: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Latest earlier observed model/effort of the same conversation agent, same turn first.

        A subagent may run another model than its parent, so its usage inherits
        only from its own hooks (a Codex subagent's hooks carry its agent_id).
        """
        if not isinstance(session_id, str) or not session_id:
            return (None, None)

        def latest(value: str, attribution: str) -> str | None:
            found = db.execute(
                f"SELECT ef.{value} FROM event_facts ef JOIN events e ON e.event_id = ef.event_id "
                f"WHERE ef.session_id = ? AND ef.agent_id IS ? AND ef.provider IS ? "
                f"AND ef.{attribution} = 'observed' AND ef.{value} IS NOT NULL AND e.rowid < ? "
                f"ORDER BY (ef.turn_id IS ?) DESC, e.rowid DESC LIMIT 1",
                (session_id, agent_id, provider, before_rowid, turn_id),
            ).fetchone()
            return found[0] if found else None

        return (
            latest("model", "model_attribution"),
            latest("reasoning_effort", "reasoning_effort_attribution"),
        )

    @staticmethod
    def _parent_turn(
        db: sqlite3.Connection, session_id: object, agent_id: str, before_rowid: int
    ) -> str | None:
        """The turn a Claude subagent ran under, from its agent.start / agent.end events."""
        if not isinstance(session_id, str) or not session_id:
            return None
        found = db.execute(
            "SELECT ef.turn_id FROM event_facts ef JOIN events e ON e.event_id = ef.event_id "
            "WHERE ef.provider = 'claude' AND ef.session_id = ? AND ef.agent_id = ? "
            "AND ef.kind IN ('agent.start', 'agent.end') AND ef.turn_id IS NOT NULL "
            "AND e.rowid < ? ORDER BY e.rowid DESC LIMIT 1",
            (session_id, agent_id, before_rowid),
        ).fetchone()
        return found[0] if found else None

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
            "offset, length(tail), last_error, error_signature FROM transcript_sources "
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
            "tail_size",
            "last_error",
            "error_signature",
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

    def transcript_source_state(self, source: Mapping[str, object]) -> tuple[object, object]:
        row = self.connection.execute(
            "SELECT tail, counters FROM transcript_sources "
            "WHERE provider=? AND session_id=? AND path=?",
            (source["provider"], source["session_id"], source["path"]),
        ).fetchone()
        if row is None:
            raise StorageError("Transcript source no longer exists")
        return row[0], row[1]

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
        if offset < 0 or len(tail) > TRANSCRIPT_TAIL_BYTES:
            raise StorageError("Invalid transcript reader state")
        serialized = json.dumps(counters, sort_keys=True) if counters is not None else None
        identifiers = tuple(
            sqlite_integer(value) if value is not None else None
            for value in (device, inode, size, mtime)
        )
        with self._transaction() as db:
            db.execute(
                "UPDATE transcript_sources SET reader=?, device=?, inode=?, size=?, mtime=?, "
                "offset=?, tail=?, counters=?, last_error=?, error_signature=? "
                "WHERE provider=? AND session_id=? AND path=?",
                (
                    reader,
                    *identifiers,
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
        except TypeError, ValueError:
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
        """Persist a fingerprint for oscillation detection.

        Capture storage holds the initial and outcome working-copy patches
        and the commit hashes needed to recover the committed diff from Git.
        """
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

    def label(
        self,
        provider: str,
        session_id: str,
        *,
        outcome: str,
        task_type: str | None,
        progress_state: str | None = None,
        reviewer_note: str | None = None,
    ) -> dict:
        """Record a manual session label.

        ``progress_state`` and ``reviewer_note`` are annotation fields added in
        schema v7.  Passing ``None`` keeps whatever a previous review stored, so
        correcting an outcome never silently discards a progress judgement.
        """
        if provider not in {"codex", "claude"} or not session_id.strip():
            raise StorageError("A supported provider and session identity are required")
        if outcome not in {"success", "partial", "failed", "abandoned", "unknown"}:
            raise StorageError("Invalid task outcome")
        if task_type is not None:
            task_type = task_type.strip()
            if not task_type or len(task_type) > 128:
                raise StorageError("Task type must contain 1..128 characters")
        if progress_state is not None and progress_state not in PROGRESS_STATES:
            raise StorageError("Invalid progress state")
        if reviewer_note is not None:
            reviewer_note = reviewer_note.strip()
            if not reviewer_note or len(reviewer_note) > 2000:
                raise StorageError("Reviewer note must contain 1..2000 characters")
        with resources.admission(self.root), self._transaction() as db:
            if not db.execute(
                "SELECT 1 FROM events WHERE session_id=? "
                "AND json_extract(envelope, '$.provider')=?",
                (session_id, provider),
            ).fetchone():
                raise StorageError("Unknown session in the selected provider")
            db.execute(
                "INSERT INTO session_labels VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, session_id) DO UPDATE SET "
                "task_outcome=excluded.task_outcome, task_type=excluded.task_type, "
                "progress_state=COALESCE(excluded.progress_state, session_labels.progress_state), "
                "reviewer_note=COALESCE(excluded.reviewer_note, session_labels.reviewer_note)",
                (provider, session_id, outcome, task_type, progress_state, reviewer_note),
            )
            stored = db.execute(
                "SELECT task_outcome, task_type, progress_state, reviewer_note "
                "FROM session_labels WHERE provider=? AND session_id=?",
                (provider, session_id),
            ).fetchone()
        return {
            "task_outcome": stored[0],
            "task_type": stored[1],
            "progress_state": stored[2],
            "reviewer_note": stored[3],
        }

    def finding_verdict(
        self,
        provider: str,
        session_id: str,
        *,
        rule: str,
        rule_version: str,
        fingerprint: str,
        verdict: str,
        note: str | None,
    ) -> dict:
        """Record a manual correctness verdict for one session-scoped finding."""
        if provider not in {"codex", "claude"} or not session_id.strip():
            raise StorageError("A supported provider and session identity are required")
        _check_verdict(rule, rule_version, fingerprint, verdict)
        note = _check_note(note)
        reviewed_at = datetime.now(UTC).isoformat()
        with resources.admission(self.root), self._transaction() as db:
            if not db.execute(
                "SELECT 1 FROM events WHERE session_id=? "
                "AND json_extract(envelope, '$.provider')=?",
                (session_id, provider),
            ).fetchone():
                raise StorageError("Unknown session in the selected provider")
            db.execute(
                "INSERT INTO finding_verdicts VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, session_id, rule, rule_version, evidence_fingerprint) "
                "DO UPDATE SET verdict=excluded.verdict, note=excluded.note, "
                "reviewed_at=excluded.reviewed_at",
                (
                    provider,
                    session_id,
                    rule,
                    rule_version,
                    fingerprint,
                    verdict,
                    note,
                    reviewed_at,
                ),
            )
        return {"verdict": verdict, "note": note, "reviewed_at": reviewed_at}

    def checkout_finding_verdict(
        self,
        checkout_id: str,
        *,
        rule: str,
        rule_version: str,
        fingerprint: str,
        verdict: str,
        note: str | None,
    ) -> dict:
        """Record a verdict for a checkout-scoped finding such as a diff oscillation."""
        if not checkout_id.strip():
            raise StorageError("A checkout identity is required")
        _check_verdict(rule, rule_version, fingerprint, verdict)
        note = _check_note(note)
        reviewed_at = datetime.now(UTC).isoformat()
        with resources.admission(self.root), self._transaction() as db:
            db.execute(
                "INSERT INTO checkout_finding_verdicts VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(checkout_id, rule, rule_version, evidence_fingerprint) "
                "DO UPDATE SET verdict=excluded.verdict, note=excluded.note, "
                "reviewed_at=excluded.reviewed_at",
                (checkout_id, rule, rule_version, fingerprint, verdict, note, reviewed_at),
            )
        return {"verdict": verdict, "note": note, "reviewed_at": reviewed_at}

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
        from agent_watchdog.capture import stream_identity
        from agent_watchdog.capture_store import CAPTURE_FILENAME, CaptureStore

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
            deleted_capture = CaptureStore(self.root / CAPTURE_FILENAME).purge(provider, session_id)
            if not event_ids and not deleted_capture:
                raise StorageError("Unknown session in the selected provider")
            for event_id in event_ids:
                self._delete(event_id)
            db.execute(
                "DELETE FROM session_labels WHERE provider=? AND session_id=?",
                (provider, session_id),
            )
            db.execute(
                "DELETE FROM finding_verdicts WHERE provider=? AND session_id=?",
                (provider, session_id),
            )
            for table in SESSION_TABLES:
                db.execute(
                    f"DELETE FROM {table} WHERE provider=? AND session_id=?", (provider, session_id)
                )
            if provider == "codex":
                db.execute("DELETE FROM codex_state_dirty WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM codex_attribution_dirty WHERE session_id=?", (session_id,))
            for key in [key for key in self.touched if key[:2] == (provider, session_id)]:
                del self.touched[key]
            db.execute(
                "DELETE FROM transcript_sources WHERE provider=? AND session_id=?",
                (provider, session_id),
            )
            if provider == "codex" and deleted_capture:
                # A parent hook can name a rollout whose filename identifies a child.
                paths = db.execute("SELECT path FROM transcript_sources WHERE provider='codex'")
                db.executemany(
                    "DELETE FROM transcript_sources WHERE provider='codex' AND path=?",
                    [
                        (name,)
                        for (name,) in paths
                        if stream_identity(provider, Path(name)) == (session_id, "main")
                    ],
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

    def maintain(
        self,
        *,
        now: datetime | None = None,
        deadline: float | None = None,
        needed: int = 65536,
    ) -> MaintenanceResult:
        with resources.admission(self.root):
            return self._maintain(now or datetime.now(UTC), needed, deadline=deadline)

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
        for table in ("artifacts", "receipts", "event_facts", "events"):
            self.connection.execute(f"DELETE FROM {table} WHERE event_id=?", (event_id,))

    def _prune_session_rows(self) -> None:
        """Drop session state and findings whose events retention has deleted."""
        for table in SESSION_TABLES:
            self.connection.execute(
                f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM events e "
                f"WHERE e.session_id = {table}.session_id AND e.provider = {table}.provider)"
            )
        for table in ("codex_state_dirty", "codex_attribution_dirty"):
            self.connection.execute(
                f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM events e "
                f"WHERE e.provider='codex' AND e.session_id={table}.session_id)"
            )
        self.touched = {
            key: state
            for key, state in self.touched.items()
            if self.connection.execute(
                "SELECT 1 FROM session_state WHERE provider=? AND session_id=? AND agent_id=?", key
            ).fetchone()
        }

    def _reclaim(self) -> None:
        """Finish explicit purge reclamation using the same bounded primitives."""
        progress = MaintenanceState(phase="artifacts", now=datetime.now(UTC))
        previous = self.maintenance_state
        self.maintenance_state = progress
        try:
            while progress.phase != "space":
                result = self._maintenance_step(progress.now or datetime.now(UTC), 0)
                if result is not None and result.pending:
                    break
        finally:
            self.maintenance_state = previous

    def _maintain(
        self, now: datetime, needed: int, *, deadline: float | None = None
    ) -> MaintenanceResult:
        started = time.monotonic()
        budget = self.maintenance_budget
        deadlines = [value for value in (deadline, self.maintenance_deadline) if value is not None]
        if budget is not None:
            deadlines.append(started + max(0.0, budget.remaining))
        try:
            return self._run_maintenance(
                now, needed, deadline=min(deadlines) if deadlines else None
            )
        finally:
            if budget is not None:
                budget.remaining = max(0.0, budget.remaining - (time.monotonic() - started))

    def _run_maintenance(
        self, now: datetime, needed: int, *, deadline: float | None
    ) -> MaintenanceResult:
        db = self.connection
        if deadline is not None and time.monotonic() >= deadline:
            return MaintenanceResult(pending=True)
        if deadline is not None:
            # Finish an admitted small mutation atomically, even if parsing consumes
            # the final milliseconds. Interrupting its COMMIT would parse it twice.
            db.set_progress_handler(
                lambda: int(not db.in_transaction and time.monotonic() >= deadline), 1000
            )
        try:
            while deadline is None or time.monotonic() < deadline:
                result = self._maintenance_step(now, needed)
                if result is not None:
                    return result
        except sqlite3.OperationalError as error:
            if deadline is None or "interrupt" not in str(error).lower():
                raise
        finally:
            db.set_progress_handler(None, 0)
        return MaintenanceResult(pending=True)

    def _maintenance_window(self) -> int | None:
        progress = self.maintenance_state
        rows = self.connection.execute(
            "SELECT rowid FROM events WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
            (progress.cursor, progress.ceiling, MAINTENANCE_CHUNK),
        ).fetchall()
        return rows[-1][0] if rows else None

    def _maintenance_step(self, now: datetime, needed: int) -> MaintenanceResult | None:
        """Run one finite SQL, file, or vacuum unit; callers check time between units."""
        progress, db = self.maintenance_state, self.connection
        phase = progress.phase
        if phase == "start":
            progress.now = now
            progress.ceiling = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[
                0
            ]
            progress.phase = "metrics"
            return None
        now = progress.now or now
        if phase in {"metrics", "content"}:
            return self._maintenance_retention(now)
        if phase == "pressure_content":
            return self._maintenance_pressure_content()
        if phase == "session_select":
            end = self._maintenance_window()
            if end is None:
                progress.cursor = 0
                if progress.session_selected:
                    progress.phase = "session_delete"
                else:
                    # Publishers and pin changes can add candidates between slices.
                    remaining = db.execute(
                        f"SELECT 1 FROM events WHERE {_UNPINNED} LIMIT 1"
                    ).fetchone()
                    progress.__dict__.update(MaintenanceState().__dict__)
                    if remaining:
                        return None
                    return MaintenanceResult(pending=False, exhausted=True)
                return None
            where = f"events.rowid>? AND events.rowid<=? AND {_UNPINNED}"
            row = db.execute(
                f"SELECT events.session_id, f.received_at_us FROM events "
                f"JOIN event_facts f ON f.event_id=events.event_id WHERE {where} "
                "ORDER BY f.received_at_us,events.rowid LIMIT 1",
                (progress.cursor, end),
            ).fetchone()
            if row and (progress.oldest is None or row[1] < progress.oldest):
                progress.session, progress.oldest = row
                progress.session_selected = True
            progress.cursor = end
            return None
        if phase == "session_delete":
            rows = db.execute(
                f"SELECT event_id FROM events WHERE session_id IS ? AND {_UNPINNED} LIMIT ?",
                (progress.session, MAINTENANCE_CHUNK),
            ).fetchall()
            if rows:
                with self._transaction():
                    for (event_id,) in rows:
                        self._delete(event_id)
            else:
                progress.session = progress.oldest = None
                progress.session_selected = False
                progress.phase = "prune_pressure"
            return None
        if phase in {"prune", "prune_pressure"}:
            tables = (*SESSION_TABLES, "codex_state_dirty")
            if progress.prune_table == len(tables):
                progress.prune_table = 0
                progress.phase = "transcripts" if phase == "prune" else "artifacts"
                progress.after_account = (
                    "pressure_content" if phase == "prune" else "session_select"
                )
                return None
            table = tables[progress.prune_table]
            provider = "'codex'" if table == "codex_state_dirty" else f"{table}.provider"
            with self._transaction():
                cursor = db.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} "
                    f"WHERE NOT EXISTS (SELECT 1 FROM events e WHERE "
                    f"e.session_id={table}.session_id AND e.provider={provider}) LIMIT ?)"
                    + (
                        " RETURNING provider, session_id, agent_id"
                        if table == "session_state"
                        else ""
                    ),
                    (MAINTENANCE_CHUNK,),
                )
                if table == "session_state":
                    rows = cursor.fetchall()
                    removed = len(rows)
                else:
                    rows, removed = [], cursor.rowcount
            for key in rows:
                self.touched.pop(tuple(key), None)
            if removed < MAINTENANCE_CHUNK:
                progress.prune_table += 1
            return None
        if phase == "transcripts":
            removed = 0
            if self.limits.content_days:
                with self._transaction():
                    removed = db.execute(
                        "DELETE FROM transcript_sources WHERE rowid IN (SELECT rowid FROM "
                        "transcript_sources WHERE last_seen<? LIMIT ?)",
                        (
                            (now - timedelta(days=self.limits.content_days)).isoformat(),
                            MAINTENANCE_CHUNK,
                        ),
                    ).rowcount
            if removed < MAINTENANCE_CHUNK:
                progress.phase = "temporary"
            return None
        if phase in {"temporary", "artifacts", "account"}:
            directories = (
                (self.root, self.root / "artifacts", self.root / "quarantine")
                if phase == "temporary"
                else (self.root if phase == "account" else self.root / "artifacts",)
            )
            if progress.directory >= len(directories):
                progress.directory, progress.files = 0, None
                if phase == "temporary":
                    progress.phase = "artifacts"
                elif phase == "artifacts":
                    progress.phase = "vacuum"
                else:
                    available = resources.headroom(
                        self.limits, progress.usage, shutil.disk_usage(self.root).free
                    )
                    progress.usage = 0
                    if available >= needed:
                        progress.__dict__.update(MaintenanceState().__dict__)
                        return MaintenanceResult(pending=False, space_available=True)
                    progress.phase = progress.after_account
                return None
            if progress.files is None:
                progress.files = _maintenance_files(
                    directories[progress.directory], recursive=phase == "account"
                )
            path = next(progress.files, None)
            if path is None:
                progress.files = None
                progress.directory += 1
                return None
            try:
                if phase == "account":
                    if not path.is_symlink() and path.is_file():
                        progress.usage += path.stat().st_size
                elif phase == "temporary":
                    if path.suffix == ".tmp" and path.stat().st_mtime < now.timestamp() - 3600:
                        path.unlink()
                elif (
                    path.suffix == ".bin"
                    and len(path.stem) == 64
                    and all(char in "0123456789abcdef" for char in path.stem)
                    and not db.execute(
                        "SELECT 1 FROM artifacts WHERE digest=? LIMIT 1", (path.stem,)
                    ).fetchone()
                ):
                    path.unlink()
            except FileNotFoundError:
                pass
            return None
        if phase == "vacuum":
            if db.execute("PRAGMA freelist_count").fetchone()[0]:
                db.execute(f"PRAGMA incremental_vacuum({MAINTENANCE_CHUNK})").fetchall()
            else:
                progress.phase = "checkpoint"
            return None
        if phase == "checkpoint":
            # Never wait for a reader while holding the daemon's maintenance budget.
            db.execute("PRAGMA busy_timeout=0")
            try:
                busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                db.execute("PRAGMA busy_timeout=50")
            if busy:
                return MaintenanceResult(pending=True)
            progress.phase = progress.after_reclaim
            return None
        if phase == "space":
            if self.limits.project_bytes:
                progress.phase = "account"
                return None
            # No quota: nothing to sum, so skip the directory walk. A full disk
            # refuses admission; it never deletes stored data.
            free = shutil.disk_usage(self.root).free
            space = resources.headroom(self.limits, 0, free) >= needed
            progress.__dict__.update(MaintenanceState().__dict__)
            return MaintenanceResult(pending=False, space_available=space, exhausted=not space)
        raise StorageError(f"Unknown maintenance phase: {phase}")

    def _maintenance_retention(self, now: datetime) -> None:
        progress, db = self.maintenance_state, self.connection
        metrics = progress.phase == "metrics"
        days = self.limits.metrics_days if metrics else self.limits.content_days
        rows = []
        if days:
            where = (
                f"events.rowid<=? AND (f.received_at_us,events.rowid)>(?,?) "
                f"AND {_UNPINNED} AND f.received_at_us<?"
            )
            parameters: list = [
                progress.ceiling,
                progress.cursor_time,
                progress.cursor,
                facts.epoch_us((now - timedelta(days=days)).isoformat()),
            ]
            if not metrics:
                where += f" AND {_HAS_CONTENT}"
                if self.limits.metrics_days:
                    where += " AND f.received_at_us>=?"
                    parameters.append(
                        facts.epoch_us((now - timedelta(days=self.limits.metrics_days)).isoformat())
                    )
            parameters.append(MAINTENANCE_CHUNK)
            columns = "events.rowid, f.received_at_us, events.event_id"
            if not metrics:
                columns += ", events.envelope"
            rows = db.execute(
                f"SELECT {columns} FROM events JOIN event_facts f "
                f"ON f.event_id=events.event_id WHERE {where} "
                "ORDER BY f.received_at_us,events.rowid LIMIT ?",
                parameters,
            ).fetchall()
            if not metrics:
                bounded, size = [], 0
                for row in rows:
                    document_bytes = len(row[3].encode())
                    if bounded and size + document_bytes > MAINTENANCE_CONTENT_BYTES:
                        break
                    bounded.append(row)
                    size += document_bytes
                rows = bounded
        if not rows:
            progress.cursor, progress.cursor_time = 0, SQLITE_INTEGER_MIN
            progress.phase = "content" if metrics else "prune"
            return
        with self._transaction():
            for row in rows:
                if metrics:
                    self._delete(row[2])
                else:
                    self._strip_content(row[2], row[3])
        progress.cursor, progress.cursor_time = rows[-1][:2]

    def _maintenance_pressure_content(self) -> None:
        progress, db = self.maintenance_state, self.connection
        rows = db.execute(
            f"SELECT events.rowid, f.received_at_us, events.event_id, events.envelope "
            f"FROM events JOIN event_facts f ON f.event_id=events.event_id "
            f"WHERE (f.received_at_us,events.rowid)>(?,?) AND events.rowid<=? "
            f"AND {_UNPINNED} AND {_HAS_CONTENT} "
            "ORDER BY f.received_at_us,events.rowid LIMIT 1",
            (progress.cursor_time, progress.cursor, progress.ceiling),
        ).fetchone()
        if not rows:
            progress.cursor, progress.cursor_time = 0, SQLITE_INTEGER_MIN
            progress.phase = "session_select"
            return
        with self._transaction():
            self._strip_content(rows[2], rows[3])
        progress.cursor, progress.cursor_time = rows[:2]
        progress.phase = "artifacts"
        progress.after_account = "pressure_content"

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
