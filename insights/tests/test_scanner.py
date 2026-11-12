"""Tests for incremental file scanning."""

from pathlib import Path

from wdi.db.repository import ImportState
from wdi.ingest.scanner import pending_chunk, read_complete_lines


def _state(size: int, mtime: float, offset: int) -> ImportState:
    return ImportState(
        machine_id="m1",
        source_path="x.jsonl",
        file_size=size,
        file_mtime=mtime,
        byte_offset=offset,
        last_imported_at="2026-01-01T00:00:00+00:00",
    )


def test_new_file_parsed_from_start() -> None:
    chunk = pending_chunk(None, size=100, mtime=1.0)

    assert chunk is not None
    assert chunk.start_offset == 0
    assert not chunk.full_reparse


def test_unchanged_file_skipped() -> None:
    assert pending_chunk(_state(100, 1.0, 100), size=100, mtime=1.0) is None


def test_grown_file_parsed_from_stored_offset() -> None:
    chunk = pending_chunk(_state(100, 1.0, 90), size=150, mtime=2.0)

    assert chunk is not None
    assert chunk.start_offset == 90
    assert not chunk.full_reparse


def test_shrunk_file_triggers_full_reparse() -> None:
    chunk = pending_chunk(_state(100, 1.0, 100), size=50, mtime=2.0)

    assert chunk is not None
    assert chunk.start_offset == 0
    assert chunk.full_reparse


def test_read_complete_lines_skips_trailing_partial(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":2}\n{"part')

    lines, offset = read_complete_lines(path, 0)

    assert lines == [b'{"a":1}', b'{"b":2}']
    assert offset == len(b'{"a":1}\n{"b":2}\n')

    path.write_bytes(b'{"a":1}\n{"b":2}\n{"part":3}\n')
    lines, new_offset = read_complete_lines(path, offset)
    assert lines == [b'{"part":3}']
    assert new_offset == path.stat().st_size


def test_read_complete_lines_no_newline_returns_nothing(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"unfinished"')

    lines, offset = read_complete_lines(path, 0)

    assert lines == []
    assert offset == 0
