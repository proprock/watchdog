"""Preserve hook-named transcript records without interpreting their contents."""

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from compression import zstd
from dataclasses import dataclass
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
    hook_session: str

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
    side_file: str = ""
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
    for base, dirs, names in os.walk(directory):
        root = Path(base)
        dirs[:] = sorted(
            name
            for name in dirs
            if not (root / name).is_symlink() and not (root / name).is_junction()
        )
        for name in sorted(names):
            path = root / name
            if not path.is_symlink() and path.is_file():
                yield path


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
        "source_path, source_kind, head_len, head_digest, byte_offset, record_count, raw_bytes, "
        "first_seen, last_growth, last_read, state) "
        "VALUES (?, ?, ?, ?, ?, ?, 'hook', 0, ?, 0, 0, 0, ?, ?, ?, 'open') RETURNING *",
        (
            *source.key,
            generation,
            source.hook_session,
            str(source.path),
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


def _visit(
    db: sqlite3.Connection,
    source: Source,
    row: sqlite3.Row | None,
    stamp: str,
    available: int,
) -> tuple[bool, int]:
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
    except FileNotFoundError:
        available = _reserve(available)
        if row is None:
            row = _new_stream(db, source, 0, stamp)
        db.execute(
            "UPDATE streams SET state='missing', last_read=?, last_error=NULL WHERE stream_pk=?",
            (stamp, row["stream_pk"]),
        )
        return False, available

    raw_bytes = end - offset - skipped_bytes
    available = _reserve(available, raw_bytes)
    if row is None or replaced:
        row = _new_stream(db, source, 0 if row is None else row["generation"] + 1, stamp)
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
        "UPDATE streams SET source_path=?, head_len=?, head_digest=?, byte_offset=?, "
        "record_count=record_count+?, raw_bytes=raw_bytes+?, skipped_lines=skipped_lines+?, "
        "skipped_bytes=skipped_bytes+?, last_growth=?, last_read=?, state='open', last_error=NULL "
        "WHERE stream_pk=?",
        (
            str(source.path),
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
    return end != offset, available


def _save_side_file(
    db: sqlite3.Connection,
    source: Source,
    path: Path,
    directory: Path,
    stamp: str,
    available: int,
) -> tuple[bool, int]:
    signature = _signature(path)
    size = signature[2]
    remaining = _reserve(available, size)
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size or _signature(path) != signature:
        return False, available
    rel = path.relative_to(directory).as_posix()
    key = source.provider, source.session, rel, _digest(data)
    if db.execute(
        "SELECT 1 FROM side_files WHERE provider=? AND session_id=? AND rel_path=? AND digest=?",
        key,
    ).fetchone():
        return False, available
    db.execute(
        "INSERT INTO side_files (provider, session_id, rel_path, digest, raw_bytes, codec, data, "
        "captured_at) VALUES (?, ?, ?, ?, ?, 'zstd', ?, ?)",
        (*key, size, zstd.compress(data, level=ZSTD_LEVEL), stamp),
    )
    return True, remaining


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
    rows = {
        (row["provider"], row["session_id"], row["stream_id"]): row
        for row in db.execute("SELECT * FROM streams ORDER BY generation")
    }
    side_dirs = []
    for source in list(sources.values()):
        if source.provider == "claude" and source.stream == "main":
            directory = source.path.with_suffix("")
            side_dirs.append((source, directory))
            subagents = directory / "subagents"
            if directory.is_symlink() or directory.is_junction():
                continue
            for path in _files(subagents):
                identity = stream_identity("claude", path)
                if path.parent != subagents or identity is None:
                    continue
                session, stream = identity
                child = Source("claude", session, stream, path, source.hook_session)
                sources.setdefault(child.key, child)
    background_first = cursor.background_first
    cursor.background_first = not background_first
    if background_first:
        available = _background(
            db, side_dirs, result, now, expired, available, target_bytes, cursor
        )
    for key in sorted(sources, key=lambda key: rows[key]["last_read"] if key in rows else ""):
        if expired():
            result.pending = True
            return
        source, row = sources[key], rows.get(key)
        try:
            progressed, available = _visit(db, source, row, stamp, available)
            result.progressed |= progressed
        except QuotaExceeded:
            raise
        except (OSError, zstd.ZstdError, ValueError) as error:
            available = _reserve(available)
            if row is None:
                row = _new_stream(db, source, 0, stamp)
            db.execute(
                "UPDATE streams SET last_read=?, last_error=? WHERE stream_pk=?",
                (stamp, type(error).__name__, row["stream_pk"]),
            )
            result.degraded = True
    if not background_first:
        _background(db, side_dirs, result, now, expired, available, target_bytes, cursor)


def _background(
    db: sqlite3.Connection,
    side_dirs: list[tuple[Source, Path]],
    result: CaptureResult,
    now: datetime,
    expired: Callable[[], bool],
    available: int,
    target_bytes: int,
    cursor: CaptureCursor,
) -> int:
    for row in db.execute(
        "SELECT s.stream_pk, s.last_growth, SUM(length(t.line)+1) AS size, "
        "MAX(length(t.line)+1) AS largest FROM streams s "
        "JOIN tail t USING (stream_pk) GROUP BY s.stream_pk "
        "HAVING size >= ? OR s.last_growth <= ? ORDER BY s.last_read",
        (target_bytes, (now - IDLE_AGE).isoformat()),
    ).fetchall():
        if expired():
            result.pending = True
            return available
        available = _reserve(available, min(max(target_bytes, row["largest"]), row["size"]))
        seal_tail(
            db,
            row["stream_pk"],
            target_bytes=target_bytes,
            idle=datetime.fromisoformat(row["last_growth"]) <= now - IDLE_AGE,
        )
        result.progressed = True
    side_files = sorted(
        (str(path), path, directory, source)
        for source, directory in side_dirs
        for path in _files(directory)
        if path.parent != directory / "subagents" or not _SUBAGENT.fullmatch(path.name)
    )
    split = next((i for i, item in enumerate(side_files) if item[0] > cursor.side_file), 0)
    for key, path, directory, source in side_files[split:] + side_files[:split]:
        if expired():
            result.pending = True
            break
        cursor.side_file = key
        try:
            progressed, available = _save_side_file(
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
    return available
