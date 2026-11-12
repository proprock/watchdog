"""Incremental file scanning: decide what part of an append-only JSONL needs parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wdi.db.repository import ImportState


@dataclass(slots=True)
class PendingChunk:
    start_offset: int
    # True when the stored state no longer matches the file (shrunk or rewritten):
    # existing rows for the stream must be deleted and the file parsed from zero.
    full_reparse: bool


def pending_chunk(state: ImportState | None, size: int, mtime: float) -> PendingChunk | None:
    """Return what to parse, or None when the file is already up to date."""
    if state is None:
        return PendingChunk(start_offset=0, full_reparse=False)
    if size == state.file_size and mtime == state.file_mtime:
        return None
    if size > state.file_size:
        return PendingChunk(start_offset=state.byte_offset, full_reparse=False)
    return PendingChunk(start_offset=0, full_reparse=True)


def read_complete_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Read complete (newline-terminated) lines from offset.

    Returns the lines and the new offset pointing right after the last complete
    line. A trailing partial line is left for the next run — append-only files
    may be flushed mid-line.
    """
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset
    return data[: last_newline + 1].splitlines(), offset + last_newline + 1
