"""Versioned, read-only access to Watchdog stores."""

import json
import sqlite3
from collections.abc import Iterator
from compression import zstd
from contextlib import closing, contextmanager
from dataclasses import dataclass
from io import BytesIO
from itertools import islice
from pathlib import Path
from typing import Any, TypedDict

from agent_watchdog.capture_store import (
    CAPTURE_FILENAME,
    CHUNK_TARGET_BYTES,
    STORE_MANIFEST_FILENAME,
)
from agent_watchdog.capture_store import STORE_CONTRACT_VERSION as CONTRACT_VERSION
from agent_watchdog.config import ConfigError as ConfigError
from agent_watchdog.config import load_config, project_aliases, project_for_reference, user_paths
from agent_watchdog.events import Envelope
from agent_watchdog.facts import FACT_COLUMNS as FACT_COLUMNS
from agent_watchdog.storage import EVENTS_SCHEMA_VERSION, PENDING_SESSIONS, persisted_envelope
from agent_watchdog.storage import StorageError as StorageError

_PAGE_SIZE = 128
_EVENTS_FILENAME = "events.sqlite3"


@dataclass(frozen=True)
class StoreInfo:
    path: Path
    contract: int
    machine_id: str | None
    hostname: str | None
    os: str | None
    project_id: str | None
    root: str | None
    git_common_dir: str | None
    name: str | None
    events_schema: int | None
    capture_schema: int | None
    written_at: str | None

    @property
    def supported(self) -> bool:
        return self.contract == CONTRACT_VERSION


@dataclass(frozen=True)
class StreamInfo:
    stream_pk: int
    provider: str
    session_id: str
    stream_id: str
    generation: int
    cwd: str | None
    record_count: int
    raw_bytes: int


@dataclass(frozen=True)
class SideFileInfo:
    rel_path: str
    digest: str
    raw_bytes: int
    captured_at: str


def default_data_dir() -> Path:
    return user_paths().data


def _manifest_info(path: Path, document: object) -> StoreInfo:
    if not isinstance(document, dict):
        raise ValueError(f"Invalid store manifest at {path}")
    contract = document.get("contract")
    if not isinstance(contract, int) or isinstance(contract, bool) or contract < 1:
        raise ValueError(f"Invalid contract version in store manifest at {path}")

    def field[T](name: str, kind: type[T], *, nullable: bool = False) -> T | None:
        value = document.get(name)
        if isinstance(value, kind) and not isinstance(value, bool):
            return value
        if contract != CONTRACT_VERSION or (nullable and value is None and name in document):
            return None
        raise ValueError(f"Invalid {name} in store manifest at {path}")

    return StoreInfo(
        path=path.parent,
        contract=contract,
        machine_id=field("machine_id", str),
        hostname=field("hostname", str),
        os=field("os", str),
        project_id=field("project_id", str),
        root=field("root", str),
        git_common_dir=field("git_common_dir", str, nullable=True),
        name=field("name", str),
        events_schema=field("events_schema", int),
        capture_schema=field("capture_schema", int),
        written_at=field("written_at", str),
    )


def stores(data_dir: Path) -> list[StoreInfo]:
    """Discover stores with manifests, including unsupported versions."""
    result = []
    for path in sorted((data_dir / "projects").glob(f"*/{STORE_MANIFEST_FILENAME}")):
        try:
            with path.open(encoding="utf-8") as stream:
                document = json.load(stream)
        except FileNotFoundError:
            continue
        except ValueError as error:
            raise ValueError(f"Invalid store manifest at {path}") from error
        result.append(_manifest_info(path, document))
    return result


@contextmanager
def _readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


def _check_contract(info: StoreInfo) -> None:
    if not info.supported:
        raise ValueError(f"Unsupported store contract {info.contract}; expected {CONTRACT_VERSION}")


@contextmanager
def _open(
    info: StoreInfo, filename: str, expected_schema: int | None
) -> Iterator[sqlite3.Connection]:
    _check_contract(info)
    path = info.path / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    with _readonly_database(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != expected_schema:
            raise ValueError(
                f"{filename} schema {version} differs from manifest ({expected_schema})"
            )
        yield db


@contextmanager
def open_events(info: StoreInfo) -> Iterator[sqlite3.Connection]:
    with _open(info, _EVENTS_FILENAME, info.events_schema) as db:
        if db.execute("SELECT project_id FROM metadata").fetchall() != [(info.project_id,)]:
            raise ValueError("Events database belongs to a different project")
        yield db


@contextmanager
def open_capture(info: StoreInfo) -> Iterator[sqlite3.Connection]:
    with _open(info, CAPTURE_FILENAME, info.capture_schema) as db:
        yield db


def _has_capture(info: StoreInfo) -> bool:
    _check_contract(info)
    return (info.path / CAPTURE_FILENAME).is_file()


def _event_rows(info: StoreInfo, *, facts: bool, after_rowid: int) -> Iterator[dict[str, Any]]:
    _check_contract(info)
    if not (info.path / _EVENTS_FILENAME).is_file():
        return
    with open_events(info) as db:
        high_water = db.execute("SELECT coalesce(max(rowid), 0) FROM events").fetchone()[0]
    columns = (
        FACT_COLUMNS if facts else ("event_id", "received_at", "session_id", "kind", "envelope")
    )
    alias = "f" if facts else "e"
    selected = ", ".join(f"{alias}.{name}" for name in columns)
    joined = " JOIN event_facts AS f ON f.event_id = e.event_id" if facts else ""
    while after_rowid < high_water:
        with open_events(info) as db:
            page = db.execute(
                f"SELECT e.rowid, {selected} FROM events AS e{joined} "
                "WHERE e.rowid > ? AND e.rowid <= ? ORDER BY e.rowid LIMIT ?",
                (after_rowid, high_water, _PAGE_SIZE),
            ).fetchall()
        for row in page:
            yield dict(zip(("event_rowid", *columns), row, strict=True))
        if len(page) < _PAGE_SIZE:
            return
        after_rowid = page[-1][0]


def events(info: StoreInfo, after_rowid: int = 0) -> Iterator[dict[str, Any]]:
    """Read bounded event pages, releasing each transaction before yielding."""
    yield from _event_rows(info, facts=False, after_rowid=after_rowid)


def event_facts(info: StoreInfo, after_rowid: int = 0) -> Iterator[dict[str, Any]]:
    """Read facts in parent-event order, independently of projection rowids."""
    yield from _event_rows(info, facts=True, after_rowid=after_rowid)


def _snapshot_rows(info: StoreInfo, table: str, minimum_schema: int) -> Iterator[dict[str, Any]]:
    if not _has_capture(info):
        return
    # Open even old schemas to validate the manifest against the database.
    with open_capture(info) as db:
        if info.capture_schema is None or info.capture_schema < minimum_schema:
            return
        high_water = db.execute(f"SELECT coalesce(max(rowid), 0) FROM {table}").fetchone()[0]
    after = 0
    while after < high_water:
        with open_capture(info) as db:
            db.row_factory = sqlite3.Row
            page = db.execute(
                f"SELECT rowid AS _read_rowid, * FROM {table} "
                "WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                (after, high_water, _PAGE_SIZE),
            ).fetchall()
        for row in page:
            values = dict(row)
            after = values.pop("_read_rowid")
            if table == "session_env":
                for name in (
                    "initial_files_changed",
                    "initial_insertions",
                    "initial_deletions",
                    "initial_untracked",
                ):
                    values.setdefault(name, None)
            yield values
        if len(page) < _PAGE_SIZE:
            return


def session_env(info: StoreInfo) -> Iterator[dict[str, Any]]:
    """Yield captured environment rows, preserving serialized JSON."""
    yield from _snapshot_rows(info, "session_env", 2)


def checkout_states(info: StoreInfo) -> Iterator[dict[str, Any]]:
    """Yield captured checkout outcomes."""
    yield from _snapshot_rows(info, "checkout_states", 3)


def blob_bytes(info: StoreInfo, digest: str) -> bytes:
    """Return original captured blob bytes, or raise KeyError when unavailable."""
    if not _has_capture(info):
        raise KeyError(digest)
    with open_capture(info) as db:
        if info.capture_schema is None or info.capture_schema < 2:
            raise KeyError(digest)
        row = db.execute("SELECT codec, data FROM blobs WHERE digest = ?", (digest,)).fetchone()
    if row is None:
        raise KeyError(digest)
    return _decompress(*row)


def streams(info: StoreInfo) -> Iterator[StreamInfo]:
    """List the latest generation of each logical stream."""
    if not _has_capture(info):
        return
    after = ("", "", "")
    while True:
        with open_capture(info) as db:
            page = db.execute(
                """
                SELECT stream_pk, provider, session_id, stream_id, generation, cwd,
                       record_count, raw_bytes
                FROM streams AS s
                WHERE (provider, session_id, stream_id) > (?, ?, ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM streams AS newer
                      WHERE (newer.provider, newer.session_id, newer.stream_id) =
                            (s.provider, s.session_id, s.stream_id)
                        AND newer.generation > s.generation
                  )
                ORDER BY provider, session_id, stream_id
                LIMIT ?
                """,
                (*after, _PAGE_SIZE),
            ).fetchall()
        for row in page:
            yield StreamInfo(*row)
        if len(page) < _PAGE_SIZE:
            return
        after = tuple(page[-1][1:4])


def _decompress(codec: str, data: bytes) -> bytes:
    if codec != "zstd":
        raise ValueError(f"Unsupported capture codec {codec!r}")
    return zstd.decompress(data)


def _tail_page(db: sqlite3.Connection, stream_pk: int, start: int, end: int) -> list[bytes]:
    page: list[bytes] = []
    size = 0
    with closing(
        db.execute(
            """
        SELECT record_seq, line FROM tail
        WHERE stream_pk = ? AND record_seq >= ? AND record_seq < ?
        ORDER BY record_seq LIMIT ?
        """,
            (stream_pk, start, end, _PAGE_SIZE),
        )
    ) as rows:
        for seq, line in rows:
            if seq != start + len(page):
                break
            page.append(line)
            size += len(line) + 1
            if size >= CHUNK_TARGET_BYTES:
                break
    return page


def _stream_state(db: sqlite3.Connection, stream_pk: int) -> tuple | None:
    return db.execute(
        """
        SELECT provider, session_id, stream_id, generation, first_seen, record_count
        FROM streams WHERE stream_pk = ?
        """,
        (stream_pk,),
    ).fetchone()


def lines(info: StoreInfo, stream_pk: int, start_record: int = 0) -> Iterator[bytes]:
    """Yield zero-based captured records through the count observed at entry."""
    if start_record < 0:
        raise ValueError("start_record must be nonnegative")
    with open_capture(info) as db:
        stream = _stream_state(db, stream_pk)
    if stream is None:
        raise KeyError(f"Unknown capture stream {stream_pk}")

    identity, end = stream[:-1], stream[-1]
    cursor = start_record
    last_chunk = -1
    while cursor < end:
        with open_capture(info) as db:
            # Tail sealing moves records between tables in one writer transaction.
            db.execute("BEGIN")
            current = _stream_state(db, stream_pk)
            if current is None or current[:-1] != identity:
                raise ValueError(f"Capture stream {stream_pk} changed during iteration")
            chunk = db.execute(
                """
                SELECT chunk_seq, first_record, record_count, codec, data FROM chunks
                WHERE stream_pk = ? AND chunk_seq > ? AND first_record + record_count > ?
                ORDER BY chunk_seq LIMIT 1
                """,
                (stream_pk, last_chunk, cursor),
            ).fetchone()
            if chunk is None:
                page = _tail_page(db, stream_pk, cursor, end)
        if chunk is None:
            if not page:
                raise ValueError(f"Missing capture record {cursor} in stream {stream_pk}")
            yield from page
            cursor += len(page)
            continue
        last_chunk, first, count, codec, data = chunk
        if first > cursor:
            raise ValueError(f"Missing capture record {cursor} in stream {stream_pk}")
        stop = min(first + count, end)
        with BytesIO(_decompress(codec, data)) as raw:
            for record in islice(raw, cursor - first, stop - first):
                yield record.removesuffix(b"\n")
        cursor = stop


def side_files(info: StoreInfo, provider: str, session_id: str) -> Iterator[SideFileInfo]:
    """List metadata for all captured versions of the session's side files."""
    if not _has_capture(info):
        return
    after = ("", "")
    while True:
        with open_capture(info) as db:
            page = db.execute(
                """
                SELECT rel_path, digest, raw_bytes, captured_at FROM side_files
                WHERE provider = ? AND session_id = ? AND (rel_path, digest) > (?, ?)
                ORDER BY rel_path, digest LIMIT ?
                """,
                (provider, session_id, *after, _PAGE_SIZE),
            ).fetchall()
        for row in page:
            yield SideFileInfo(*row)
        if len(page) < _PAGE_SIZE:
            return
        after = (page[-1][0], page[-1][1])


def side_file_bytes(
    info: StoreInfo, provider: str, session_id: str, rel_path: str, digest: str
) -> bytes:
    with open_capture(info) as db:
        row = db.execute(
            """
            SELECT codec, data FROM side_files
            WHERE provider = ? AND session_id = ? AND rel_path = ? AND digest = ?
            """,
            (provider, session_id, rel_path, digest),
        ).fetchone()
    if row is None:
        raise KeyError((provider, session_id, rel_path, digest))
    return _decompress(*row)


@dataclass(frozen=True)
class RegisteredProject:
    project_id: str
    name: str
    root: str
    path: Path


def registered_projects(
    config_path: Path | None = None, data_dir: Path | None = None
) -> list[RegisteredProject]:
    """Read registered aliases, including projects without collected stores."""
    paths = user_paths()
    config = load_config(config_path if config_path is not None else paths.config)
    data = data_dir if data_dir is not None else paths.data
    aliases = project_aliases(config.projects)
    return [
        RegisteredProject(
            str(project.id),
            aliases[project.id],
            str(project.root),
            data / "projects" / str(project.id),
        )
        for project in config.projects
    ]


def resolve_project(
    reference: str | None = None,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    cwd: Path | None = None,
) -> RegisteredProject:
    """Select a registered alias, UUID, or current checkout without changing registration."""
    from agent_watchdog.registry import Registry

    paths = user_paths()
    config = load_config(config_path if config_path is not None else paths.config)
    if reference is None:
        resolution = Registry(config).resolve(cwd if cwd is not None else Path.cwd())
        if resolution is not None:
            reference = str(resolution.project_id)
    project = project_for_reference(config.projects, reference) if reference is not None else None
    if project is None:
        raise StorageError(
            "Select a registered project with --project alias or its working directory"
        )
    data = data_dir if data_dir is not None else paths.data
    return RegisteredProject(
        str(project.id),
        project_aliases(config.projects)[project.id],
        str(project.root),
        data / "projects" / str(project.id),
    )


@contextmanager
def snapshot(project: RegisteredProject) -> Iterator[sqlite3.Connection]:
    """Open one consistent, read-only snapshot without requiring a manifest."""
    path = project.path / _EVENTS_FILENAME
    if not path.is_file():
        raise StorageError("Project has no collected database")
    with _readonly_database(path) as db:
        db.execute("BEGIN")
        if not 1 <= db.execute("PRAGMA user_version").fetchone()[0] <= EVENTS_SCHEMA_VERSION:
            raise StorageError("Unsupported database schema")
        if db.execute("SELECT project_id FROM metadata").fetchall() != [(project.project_id,)]:
            raise StorageError("Database belongs to a different project")
        db.row_factory = sqlite3.Row
        yield db


class SessionLabel(TypedDict):
    task_outcome: str
    task_type: str | None
    progress_state: str | None
    reviewer_note: str | None


class SessionReview(TypedDict):
    session_id: str
    label: SessionLabel
    event_count: int
    gaps: list[str]
    events: list[Envelope]


def session_label(db: sqlite3.Connection, provider: str, session_id: str | None) -> SessionLabel:
    """Read a manual label, with defaults for absent sessions and older schemas."""
    default: SessionLabel = {
        "task_outcome": "unknown",
        "task_type": None,
        "progress_state": None,
        "reviewer_note": None,
    }
    if (
        session_id is None
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_labels'"
        ).fetchone()
    ):
        return default
    columns = {row[1] for row in db.execute("PRAGMA table_info(session_labels)")}
    annotated = "progress_state" in columns and "reviewer_note" in columns
    selection = (
        "task_outcome, task_type, progress_state, reviewer_note"
        if annotated
        else "task_outcome, task_type, NULL, NULL"
    )
    row = db.execute(
        f"SELECT {selection} FROM session_labels WHERE provider=? AND session_id=?",
        (provider, session_id),
    ).fetchone()
    if row is None:
        return default
    return {
        "task_outcome": row[0],
        "task_type": row[1],
        "progress_state": row[2],
        "reviewer_note": row[3],
    }


def findings(project: RegisteredProject) -> dict[str, Any]:
    """Read the daemon's per-session findings (WD-141) instead of re-analyzing every event.

    Unlike usage, findings are not windowed: they cover every retained event, as of
    each session's ``computed_at``.  Cross-session repeats are not merged, and one
    diff oscillation shared by several sessions of a checkout counts once.
    """
    from agent_watchdog.analysis import RULE_VERSION, analyze

    if not (project.path / _EVENTS_FILENAME).is_file():
        return {"state": "unavailable", "reason": "no_collected_database"}
    by_rule: dict[str, dict[str, int]] = {"codex": {}, "claude": {}}
    gaps: dict[str, set[str]] = {"codex": set(), "claude": set()}
    with snapshot(project) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        if schema < 8:
            return {"state": "unsupported", "reason": "schema_predates_v8"}
        if schema < 9:
            pending = db.execute(
                "SELECT COUNT(*) FROM (SELECT provider, session_id FROM events "
                "WHERE session_id IS NOT NULL GROUP BY provider, session_id)"
            ).fetchone()[0]
            analyzed = []
            as_of = None
            findings = []
        else:
            findings = db.execute(
                "SELECT f.provider, f.rule, COUNT(DISTINCT f.fingerprint) FROM session_findings f "
                "JOIN session_analysis a USING (provider, session_id) WHERE a.rule_version=? "
                "GROUP BY f.provider, f.rule ORDER BY f.provider, f.rule",
                (RULE_VERSION,),
            ).fetchall()
            analyzed = db.execute(
                "SELECT provider, gaps_json FROM session_analysis WHERE rule_version=?",
                (RULE_VERSION,),
            ).fetchall()
            as_of = db.execute(
                "SELECT MAX(computed_at) FROM session_analysis WHERE rule_version=?",
                (RULE_VERSION,),
            ).fetchone()[0]
            pending = len(db.execute(PENDING_SESSIONS, (RULE_VERSION,)).fetchall())
        for provider, rule, count in findings:
            if provider in by_rule:
                by_rule[provider][rule] = count
    for provider, document in analyzed:
        if provider in gaps:
            gaps[provider].update(json.loads(document))
    return {
        "state": "ready",
        "window": "all",
        "as_of": as_of,
        "pending_sessions": pending,
        "by_provider": {
            provider: {
                "by_rule": by_rule[provider],
                # A provider with no analyzed session reports what an empty analysis lacks.
                "gaps": sorted(gaps[provider]) or analyze([])["gaps"],
            }
            for provider in by_rule
        },
    }


def session_review(
    project: RegisteredProject, provider: str, session_ids: list[str]
) -> list[SessionReview]:
    """Read selected sessions, sanitized for explicit offline export."""
    from agent_watchdog.analysis import analyze
    from agent_watchdog.privacy import sanitize

    if provider not in {"codex", "claude"}:
        raise StorageError("Unsupported provider")
    selected = list(dict.fromkeys(session_ids))
    if not selected or any(not item.strip() for item in selected):
        raise StorageError("Select at least one session identity")
    records: list[SessionReview] = []
    with snapshot(project) as db:
        for session_id in selected:
            rows = db.execute(
                "SELECT envelope FROM events WHERE session_id=? "
                "AND json_extract(envelope, '$.provider')=? ORDER BY rowid",
                (session_id, provider),
            ).fetchall()
            if not rows:
                raise StorageError("Unknown session in the selected project/provider")
            events = [sanitize(persisted_envelope(row[0])) for row in rows]
            label = session_label(db, provider, session_id)
            report = analyze(events)
            if label["task_outcome"] != "unknown":
                report["gaps"] = [gap for gap in report["gaps"] if gap != "task_outcome_unknown"]
            gaps = report["gaps"]
            if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
                raise StorageError("Invalid analysis gaps")
            records.append(
                {
                    "session_id": session_id,
                    "label": label,
                    "event_count": len(events),
                    "gaps": gaps,
                    "events": events,
                }
            )
    return records
