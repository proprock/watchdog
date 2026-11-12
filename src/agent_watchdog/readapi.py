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

from agent_watchdog.capture_store import (
    CAPTURE_FILENAME,
    CHUNK_TARGET_BYTES,
    STORE_MANIFEST_FILENAME,
)
from agent_watchdog.capture_store import STORE_CONTRACT_VERSION as CONTRACT_VERSION
from agent_watchdog.config import user_paths

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
