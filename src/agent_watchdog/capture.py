"""Preserve hook-named transcript records without interpreting their contents."""

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from compression import zstd
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast

from agent_watchdog import resources
from agent_watchdog.capture_store import (
    CAPTURE_FILENAME,
    CHUNK_TARGET_BYTES,
    ZSTD_LEVEL,
    CaptureStore,
    seal_tail,
)
from agent_watchdog.config import Limits
from agent_watchdog.storage import QuotaExceeded, Store
from agent_watchdog.transcripts import MAX_LINE_BYTES, _line_end, _read_window, _signature

HEAD_BYTES = 4096
SOURCE_AGE = timedelta(hours=1)
IDLE_AGE = timedelta(hours=24)
WRITE_OVERHEAD = 65536
TICK_BUDGET = 0.1
_ROLLOUT = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(?:\.zst)?",
    re.IGNORECASE,
)
_SUBAGENT = re.compile(r"agent-([0-9a-f]+)\.jsonl", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    provider: str
    session: str
    stream: str
    path: Path
    hook_session: str | None
    kind: str = "hook"
    cwd: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.provider, self.session, self.stream


@dataclass
class CaptureResult:
    progressed: bool = False
    pending: bool = False
    degraded: bool = False
    unrecognized_name: int = 0


@dataclass
class CaptureCursor:
    # Stream scheduling is persisted in last_read. Side files have no mutable
    # cursor row, so keep their directory traversal position for this daemon.
    side_walk: Iterator[tuple[Source, Path, Path]] | None = None
    side_next: tuple[Source, Path, Path] | None = None
    seal_after: int = 0
    background_first: bool = False


def stream_identity(provider: str, path: Path) -> tuple[str, str] | None:
    if provider == "codex":
        match = _ROLLOUT.fullmatch(path.name)
        return (match[1].lower(), "main") if match else None
    if provider != "claude" or path.suffix != ".jsonl" or not path.stem:
        return None
    if path.parent.name == "subagents":
        match = _SUBAGENT.fullmatch(path.name)
        return (path.parent.parent.name, match[1].lower()) if match else None
    return path.stem, "main"


def _files(directory: Path) -> Iterator[Path]:
    if directory.is_symlink() or directory.is_junction():
        return
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction():
                    continue
                # Yield directories as scheduling points, including empty ones.
                yield path
                if entry.is_dir(follow_symlinks=False):
                    yield from _files(path)
    except FileNotFoundError:
        return


def _sources(
    store: Store, now: datetime, result: CaptureResult
) -> dict[tuple[str, str, str], Source]:
    sources = {}
    for provider, hook_session, name in store.connection.execute(
        "SELECT provider, session_id, path FROM transcript_sources WHERE last_seen >= ? "
        "ORDER BY last_seen, provider, session_id, path",
        ((now - SOURCE_AGE).isoformat(),),
    ):
        path = Path(name)
        identity = stream_identity(provider, path)
        if identity is None:
            result.unrecognized_name += 1
            continue
        session, stream = identity
        if provider == "codex" and path.suffix == ".zst":
            path = path.with_suffix("")
        if provider == "claude" and stream != "main":
            hook_session = hook_session.partition("#")[0]
        source = Source(provider, session, stream, path, hook_session)
        sources[source.key] = source
    return sources


def _open_source(path: Path) -> tuple[BinaryIO, int]:
    if path.suffix == ".zst":
        _, _, _, modified = _signature(path)
        return cast(BinaryIO, zstd.open(path, "rb")), modified
    try:
        _, _, _, modified = _signature(path)
    except FileNotFoundError:
        path = path.with_suffix(path.suffix + ".zst")
        _, _, _, modified = _signature(path)
        return cast(BinaryIO, zstd.open(path, "rb")), modified
    return path.open("rb"), modified


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reserve(available: int, raw_bytes: int = 0) -> int:
    # As with event admission, allow room for data pages, their WAL copies,
    # and transaction metadata.
    needed = WRITE_OVERHEAD + 4 * raw_bytes
    if available < needed:
        raise QuotaExceeded("Raw capture reached the project quota or disk reserve")
    return available - needed


def _new_stream(db: sqlite3.Connection, source: Source, generation: int, stamp: str) -> sqlite3.Row:
    row = db.execute(
        "INSERT INTO streams (provider, session_id, stream_id, generation, hook_session_id, "
        "source_path, source_kind, cwd, head_len, head_digest, byte_offset, record_count, "
        "raw_bytes, first_seen, last_growth, last_read, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, 0, ?, ?, ?, 'open') RETURNING *",
        (
            *source.key,
            generation,
            source.hook_session,
            str(source.path),
            source.kind,
            source.cwd,
            _digest(b""),
            stamp,
            stamp,
            stamp,
        ),
    ).fetchone()
    return row


def _records(stream: BinaryIO, offset: int) -> tuple[list[bytes], int, int, int]:
    if stream.tell() != offset:
        stream.seek(offset)
    window = _read_window(stream, 0)
    end = window.rfind(b"\n") + 1
    skipped_lines = skipped_bytes = 0
    if end:
        records = []
        for line in window[: end - 1].split(b"\n"):
            if len(line) > MAX_LINE_BYTES:
                skipped_lines += 1
                skipped_bytes += len(line) + 1
            else:
                records.append(line)
        return records, offset + end, skipped_lines, skipped_bytes
    if len(window) > MAX_LINE_BYTES:
        line_end = _line_end(stream)
        if line_end is not None:
            return [], line_end, 1, line_end - offset
    return [], offset, 0, 0


def _latest(db: sqlite3.Connection, source: Source) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM streams WHERE provider=? AND session_id=? AND stream_id=? "
        "ORDER BY generation DESC LIMIT 1",
        source.key,
    ).fetchone()


def _source_row(db: sqlite3.Connection, source: Source) -> sqlite3.Row | None:
    # Divergent files can share a stream identity. Resume the matching path.
    return db.execute(
        "SELECT * FROM streams WHERE provider=? AND session_id=? AND stream_id=? "
        "ORDER BY (source_path=?) DESC, generation DESC LIMIT 1",
        (*source.key, str(source.path)),
    ).fetchone()


def _visit(
    db: sqlite3.Connection,
    source: Source,
    row: sqlite3.Row | None,
    stamp: str,
    available: int,
    *,
    check_eof: bool = False,
) -> tuple[bool, int, bool]:
    try:
        stream, modified = _open_source(source.path)
        with stream:
            head = stream.read(HEAD_BYTES)
            replaced = row is not None and _digest(head[: row["head_len"]]) != row["head_digest"]
            if row is not None and not replaced and row["byte_offset"] > len(head):
                # A plain file seek can succeed past EOF, unlike a zstd seek.
                stream.seek(row["byte_offset"] - 1)
                replaced = not stream.read(1)
            offset = 0 if row is None or replaced else row["byte_offset"]
            lines, end, skipped_lines, skipped_bytes = _records(stream, offset)
            complete = check_eof and stream.tell() == end and not stream.read(1)
    except FileNotFoundError:
        available = _reserve(available)
        if row is None:
            row = _new_stream(db, source, 0, stamp)
        db.execute(
            "UPDATE streams SET state='missing', last_read=?, last_error=NULL WHERE stream_pk=?",
            (stamp, row["stream_pk"]),
        )
        return False, available, False

    raw_bytes = end - offset - skipped_bytes
    try:
        available = _reserve(available, raw_bytes)
    except QuotaExceeded:
        _reserve(available)
        if row is None:
            _new_stream(db, source, 0, stamp)
        else:
            db.execute(
                "UPDATE streams SET last_read=? WHERE stream_pk=?", (stamp, row["stream_pk"])
            )
        raise
    if row is None:
        row = _new_stream(db, source, 0, stamp)
    elif replaced:
        latest = _latest(db, source)
        assert latest is not None
        source = replace(
            source,
            kind=latest["source_kind"],
            hook_session=source.hook_session or row["hook_session_id"],
        )
        row = _new_stream(db, source, latest["generation"] + 1, stamp)
    pk = row["stream_pk"]
    db.executemany(
        "INSERT INTO tail (stream_pk, record_seq, line) VALUES (?, ?, ?)",
        ((pk, seq, line) for seq, line in enumerate(lines, row["record_count"])),
    )
    grew = (
        end != offset
        or len(head) > row["head_len"]
        or modified > int(datetime.fromisoformat(row["last_growth"]).timestamp() * 1e9)
    )
    db.execute(
        "UPDATE streams SET source_path=?, cwd=coalesce(cwd, ?), "
        "hook_session_id=coalesce(hook_session_id, ?), head_len=?, head_digest=?, byte_offset=?, "
        "record_count=record_count+?, raw_bytes=raw_bytes+?, skipped_lines=skipped_lines+?, "
        "skipped_bytes=skipped_bytes+?, last_growth=?, last_read=?, state='open', last_error=NULL "
        "WHERE stream_pk=?",
        (
            str(source.path),
            source.cwd,
            source.hook_session,
            len(head),
            _digest(head),
            end,
            len(lines),
            raw_bytes,
            skipped_lines,
            skipped_bytes,
            stamp if grew else row["last_growth"],
            stamp,
            pk,
        ),
    )
    return end != offset, available, complete


def _save_side_file(
    db: sqlite3.Connection,
    source: Source,
    path: Path,
    directory: Path,
    stamp: str,
    available: int,
) -> tuple[bool, int, bool]:
    signature = _signature(path)
    size = signature[2]
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        rel = path.relative_to(directory).as_posix()
        key = source.provider, source.session, rel, digest
        if db.execute(
            "SELECT 1 FROM side_files WHERE provider=? AND session_id=? "
            "AND rel_path=? AND digest=?",
            key,
        ).fetchone():
            return False, available, True
        remaining = _reserve(available, size)
        stream.seek(0)
        data = stream.read(size + 1)
    if len(data) != size or _digest(data) != digest or _signature(path) != signature:
        return False, available, False
    db.execute(
        "INSERT INTO side_files (provider, session_id, rel_path, digest, raw_bytes, codec, data, "
        "captured_at) VALUES (?, ?, ?, ?, ?, 'zstd', ?, ?)",
        (*key, size, zstd.compress(data, level=ZSTD_LEVEL), stamp),
    )
    return True, remaining, True


def capture_file(
    root: Path, limits: Limits, source: Source, *, side_directory: Path | None = None
) -> tuple[bool, bool]:
    """Capture one source window and report progress and EOF for the scan cache."""
    if not limits.capture_content:
        return False, False
    stamp = datetime.now(UTC).isoformat()
    with resources.admission(root):
        available = resources.available(root, limits)
        _reserve(available)
        with CaptureStore(root / CAPTURE_FILENAME).connect() as db:
            if side_directory is None:
                progressed, _, complete = _visit(
                    db, source, _source_row(db, source), stamp, available, check_eof=True
                )
            else:
                progressed, _, complete = _save_side_file(
                    db, source, source.path, side_directory, stamp, available
                )
    return progressed, complete


def capture(
    store: Store,
    *,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float = TICK_BUDGET,
    target_bytes: int = CHUNK_TARGET_BYTES,
    cursor: CaptureCursor | None = None,
) -> CaptureResult:
    """Run one round of capture, starting with the least recently read stream."""
    result = CaptureResult()
    if not store.limits.capture_content:
        return result
    deadline = clock() + budget_seconds
    now = now or datetime.now(UTC)
    cursor = cursor if cursor is not None else CaptureCursor()
    path = store.root / CAPTURE_FILENAME
    sources = _sources(store, now, result)
    if not sources and not path.exists():
        return result
    with resources.admission(store.root):
        available = resources.available(store.root, store.limits)
        if available < WRITE_OVERHEAD:
            result.degraded = True
            return result
        with CaptureStore(path).connect(write=True) as db:
            try:
                _capture_round(
                    db,
                    sources,
                    result,
                    now,
                    lambda: clock() >= deadline,
                    available,
                    target_bytes,
                    cursor,
                )
            except QuotaExceeded:
                result.degraded = True
    return result


def _capture_round(
    db: sqlite3.Connection,
    sources: dict[tuple[str, str, str], Source],
    result: CaptureResult,
    now: datetime,
    expired: Callable[[], bool],
    available: int,
    target_bytes: int,
    cursor: CaptureCursor,
) -> None:
    stamp = now.isoformat()
    background_first = cursor.background_first
    cursor.background_first = not background_first
    if background_first:
        available = _background(db, sources, result, now, expired, available, target_bytes, cursor)
    rows = [(source, _source_row(db, source)) for source in sources.values()]
    for source, row in sorted(rows, key=lambda item: item[1]["last_read"] if item[1] else ""):
        if expired():
            result.pending = True
            return
        available = _visit_source(db, source, row, result, stamp, available)
    if not background_first:
        _background(db, sources, result, now, expired, available, target_bytes, cursor)


def _visit_source(
    db: sqlite3.Connection,
    source: Source,
    row: sqlite3.Row | None,
    result: CaptureResult,
    stamp: str,
    available: int,
) -> int:
    try:
        progressed, available, _ = _visit(db, source, row, stamp, available)
        result.progressed |= progressed
    except QuotaExceeded:
        raise
    except (OSError, EOFError, zstd.ZstdError, ValueError) as error:
        available = _reserve(available)
        if row is None:
            row = _new_stream(db, source, 0, stamp)
        db.execute(
            "UPDATE streams SET last_read=?, last_error=? WHERE stream_pk=?",
            (stamp, type(error).__name__, row["stream_pk"]),
        )
        result.degraded = True
    return available


def _side_files(sources: list[Source]) -> Iterator[tuple[Source, Path, Path]]:
    for source in sources:
        if source.provider == "claude" and source.stream == "main":
            directory = source.path.with_suffix("")
            for path in _files(directory):
                yield source, directory, path


def _background(
    db: sqlite3.Connection,
    sources: dict[tuple[str, str, str], Source],
    result: CaptureResult,
    now: datetime,
    expired: Callable[[], bool],
    available: int,
    target_bytes: int,
    cursor: CaptureCursor,
) -> int:
    idle_since = (now - IDLE_AGE).isoformat()
    while not expired():
        row = db.execute(
            "SELECT stream_pk, SUM(length(line)+1) AS size, MAX(length(line)+1) AS largest "
            "FROM tail WHERE stream_pk > ? GROUP BY stream_pk ORDER BY stream_pk LIMIT 1",
            (cursor.seal_after,),
        ).fetchone()
        if row is None:
            cursor.seal_after = 0
            break
        if expired():
            result.pending = True
            return available
        cursor.seal_after = row["stream_pk"]
        (last_growth,) = db.execute(
            "SELECT last_growth FROM streams WHERE stream_pk=?", (cursor.seal_after,)
        ).fetchone()
        idle = last_growth <= idle_since
        if row["size"] < target_bytes and not idle:
            continue
        available = _reserve(available, min(max(target_bytes, row["largest"]), row["size"]))
        seal_tail(db, cursor.seal_after, target_bytes=target_bytes, idle=idle)
        result.progressed = True
    if cursor.side_walk is None:
        cursor.side_walk = _side_files(list(sources.values()))
    while not expired():
        if cursor.side_next is None:
            cursor.side_next = next(cursor.side_walk, None)
        if cursor.side_next is None:
            cursor.side_walk = None
            return available
        if expired():
            break
        source, directory, path = cursor.side_next
        cursor.side_next = None
        if sources.get(source.key) != source or not path.is_file():
            continue
        if path.parent == directory / "subagents" and (match := _SUBAGENT.fullmatch(path.name)):
            child = Source("claude", source.session, match[1].lower(), path, source.hook_session)
            if child.key not in sources:
                available = _visit_source(
                    db, child, _source_row(db, child), result, now.isoformat(), available
                )
            continue
        try:
            progressed, available, _ = _save_side_file(
                db, source, path, directory, now.isoformat(), available
            )
            result.progressed |= progressed
        except FileNotFoundError:
            continue
        except QuotaExceeded:
            raise
        except (OSError, zstd.ZstdError, ValueError) as error:
            available = _reserve(available)
            db.execute(
                "UPDATE streams SET last_error=? WHERE provider=? AND session_id=? "
                "AND stream_id=? AND generation=(SELECT MAX(generation) FROM streams "
                "WHERE provider=? AND session_id=? AND stream_id=?)",
                (type(error).__name__, *source.key, *source.key),
            )
            result.degraded = True
    result.pending = True
    return available
