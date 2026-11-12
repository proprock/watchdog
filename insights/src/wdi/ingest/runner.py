"""Shared per-file import skeleton: scan, parse, persist, advance import state."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from wdi.db.repository import Repository
from wdi.ingest.models import MAIN_STREAM, ImportStats, ParsedChunk
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.scanner import pending_chunk, read_complete_lines

# (lines, message index, tool index, context index, model-call index,
#  compaction generation, previous event type) -> ParsedChunk
ParseFn = Callable[[list[bytes], int, int, int, int, int, str | None], ParsedChunk]


def import_stream_file(
    repo: Repository,
    resolver: ProjectResolver,
    machine_id: str,
    path: Path,
    session_id: str,
    stream_id: str,
    agent: str,
    parse_fn: ParseFn,
) -> ImportStats | None:
    """Import pending lines of one transcript file; None when already up to date."""
    stats = ImportStats(files_seen=1)
    stat = path.stat()
    state = repo.get_import_state(machine_id, str(path))
    chunk_info = pending_chunk(state, stat.st_size, stat.st_mtime)
    if chunk_info is None:
        return None

    # Response fragments and tool results may complete an earlier import. Replaying
    # the changed stream keeps identity reconciliation identical to a fresh import.
    replay = chunk_info.start_offset > 0
    lines, new_offset = read_complete_lines(path, 0 if replay else chunk_info.start_offset)
    if (
        chunk_info.full_reparse
        or replay
        or (state is None and repo.next_idx(session_id, stream_id, "messages") > 0)
    ):
        repo.delete_stream(session_id, stream_id, drop_subagent_runs=stream_id == MAIN_STREAM)
    if not lines:
        repo.set_import_state(
            machine_id,
            str(path),
            file_size=stat.st_size,
            file_mtime=stat.st_mtime,
            byte_offset=new_offset,
        )
        return stats

    is_new_session = repo.ensure_session(session_id, machine_id, agent)
    chunk = parse_fn(lines, 0, 0, 0, 0, 0, None)

    repo.insert_messages(session_id, machine_id, stream_id, chunk.messages)
    repo.insert_tool_calls(session_id, machine_id, stream_id, chunk.tool_calls)
    repo.insert_context_events(session_id, machine_id, stream_id, chunk.context_events)
    for result in chunk.tool_results:
        repo.apply_tool_result(session_id, result)
    for subagent in chunk.subagents:
        repo.upsert_subagent_run(session_id, machine_id, subagent)

    if stream_id == MAIN_STREAM:
        fields = dict(chunk.session_fields)
        fields["source_path"] = str(path)
        repo.set_session_fields(session_id, fields)
        cwd = chunk.session_fields.get("cwd")
        project_id = resolver.resolve(cwd if isinstance(cwd, str) else None)
        if project_id is not None:
            repo.set_session_project(session_id, project_id)
    repo.recompute_session_aggregates(session_id)

    repo.set_import_state(
        machine_id,
        str(path),
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
        byte_offset=new_offset,
    )
    stats.files_imported = 1
    stats.sessions = 1 if is_new_session else 0
    stats.messages = len(chunk.messages)
    stats.tool_calls = len(chunk.tool_calls)
    stats.context_events = len(chunk.context_events)
    stats.subagent_runs = len(chunk.subagents)
    stats.warnings = chunk.warnings
    return stats
