"""Parse and persist transcripts from files or Watchdog streams."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from wdi.db.repository import Repository
from wdi.ingest.models import MAIN_STREAM, ImportStats, ParsedChunk
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.scanner import pending_chunk, read_complete_lines


class ParseFn(Protocol):
    def __call__(self, lines: list[bytes], *, msg_idx0: int, tool_idx0: int) -> ParsedChunk: ...


class TranscriptSource(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def version(self) -> float: ...
    @property
    def record_count(self) -> int | None: ...
    @property
    def fallback_root(self) -> str | None: ...
    def read_lines(self, offset: int) -> tuple[list[bytes], int]: ...


@dataclass(frozen=True)
class FileSource:
    path: Path
    size: int
    version: float
    fallback_root: None = None
    record_count: None = None

    @property
    def key(self) -> str:
        return str(self.path)

    def read_lines(self, offset: int) -> tuple[list[bytes], int]:
        return read_complete_lines(self.path, offset)


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
    session = repo.import_session(session_id)
    if session is not None and session["origin"] != "file":
        return None
    stat = path.stat()
    return import_stream(
        repo,
        resolver,
        machine_id,
        FileSource(path, stat.st_size, stat.st_mtime),
        session_id,
        stream_id,
        agent,
        parse_fn,
    )


def import_stream(
    repo: Repository,
    resolver: ProjectResolver,
    machine_id: str,
    source: TranscriptSource,
    session_id: str,
    stream_id: str,
    agent: str,
    parse_fn: ParseFn,
    *,
    watchdog: bool = False,
    full: bool = False,
) -> ImportStats | None:
    session = repo.import_session(session_id)
    if session is not None and (
        session["origin"] == "legacy" or (not watchdog and session["origin"] == "watchdog")
    ):
        return None
    main = stream_id == MAIN_STREAM
    taking_over = watchdog and session is not None and session["origin"] == "file"
    if taking_over and not main:
        return None
    state = repo.get_import_state(machine_id, source.key)
    if (
        watchdog
        and state is not None
        and source.record_count is not None
        and (source.version, source.record_count) < (state.file_mtime, state.byte_offset)
    ):
        return None
    pending = pending_chunk(None if full else state, source.size, source.version)
    if pending is None and (
        not watchdog or state is None or state.byte_offset == source.record_count
    ):
        return None

    # Later records can complete earlier ones. Both sources replay the stream.
    lines, offset = source.read_lines(0)
    chunk = parse_fn(lines, msg_idx0=0, tool_idx0=0) if lines else None
    with repo.import_transaction():
        # Recheck after reading in case a concurrent import or transfer won.
        if (
            repo.import_session(session_id) != session
            or repo.get_import_state(machine_id, source.key) != state
        ):
            return None
        is_new = chunk is not None and repo.ensure_session(session_id, machine_id, agent)
        if taking_over and chunk is not None:
            repo.replace_file_projection(session_id, session, agent)
        elif not taking_over and (
            watchdog
            or (pending is not None and (pending.full_reparse or pending.start_offset > 0))
            or (state is None and repo.next_idx(session_id, stream_id, "messages") > 0)
        ):
            repo.delete_stream(session_id, stream_id, drop_subagent_runs=main)
            if watchdog and main:
                repo.reset_watchdog_metadata(session_id, agent)

        if chunk is not None:
            if watchdog:
                repo.claim_watchdog_session(session_id, machine_id, source.key, main=main)
                if state is None:
                    repo.discard_other_watchdog_cursors(source.key, agent, session_id, stream_id)
            _persist_chunk(repo, chunk, session_id, stream_id, machine_id)
            if main or (watchdog and (is_new or taking_over)):
                fields = dict(chunk.session_fields) if main else {}
                if not watchdog:
                    fields["source_path"] = source.key
                repo.set_session_fields(session_id, fields)
                cwd = chunk.session_fields.get("cwd") if main else None
                project_id = resolver.resolve(cwd if isinstance(cwd, str) else None)
                if project_id is None and source.fallback_root is not None:
                    project_id = resolver.resolve(source.fallback_root)
                if project_id is not None:
                    repo.set_session_project(session_id, project_id)
            repo.recompute_session_aggregates(session_id)
        elif watchdog and session is not None and session["origin"] == "watchdog":
            repo.recompute_session_aggregates(session_id)
        repo.set_import_state(
            machine_id,
            source.key,
            file_size=source.size,
            file_mtime=source.version,
            byte_offset=offset,
        )
    if chunk is None:
        return ImportStats(files_seen=1)
    return ImportStats(
        files_seen=1,
        files_imported=1,
        sessions=int(is_new),
        messages=len(chunk.messages),
        tool_calls=len(chunk.tool_calls),
        context_events=len(chunk.context_events),
        subagent_runs=len(chunk.subagents),
        warnings=chunk.warnings,
    )


def _persist_chunk(
    repo: Repository, chunk: ParsedChunk, session_id: str, stream_id: str, machine_id: str
) -> None:
    repo.insert_messages(session_id, machine_id, stream_id, chunk.messages)
    repo.insert_tool_calls(session_id, machine_id, stream_id, chunk.tool_calls)
    repo.insert_context_events(session_id, machine_id, stream_id, chunk.context_events)
    for result in chunk.tool_results:
        repo.apply_tool_result(session_id, result)
    for subagent in chunk.subagents:
        repo.upsert_subagent_run(session_id, machine_id, subagent)
