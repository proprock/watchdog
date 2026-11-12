"""Repository: all SQL reads/writes go through this layer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wdi.ingest.models import (
        ContextEventRec,
        MessageRec,
        SubagentRec,
        ToolCallRec,
        ToolResultRec,
    )


@dataclass(frozen=True)
class ImportState:
    machine_id: str
    source_path: str
    file_size: int
    file_mtime: float
    byte_offset: int
    last_imported_at: str


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def ensure_machine(
        self, machine_id: str, hostname: str | None = None, os_name: str | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO machines (machine_id, hostname, os, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (machine_id) DO UPDATE SET
                    hostname = COALESCE(excluded.hostname, hostname),
                    os = COALESCE(excluded.os, os)
                """,
                (machine_id, hostname, os_name, _utcnow()),
            )

    def get_or_create_project(self, canonical_name: str) -> int:
        row = self.conn.execute(
            "SELECT project_id FROM projects WHERE canonical_name = ?", (canonical_name,)
        ).fetchone()
        if row is not None:
            return row["project_id"]
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO projects (canonical_name) VALUES (?)", (canonical_name,)
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def link_project_path(self, project_id: int, machine_id: str, path: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO project_paths (project_id, machine_id, path)
                VALUES (?, ?, ?)
                ON CONFLICT (machine_id, path) DO UPDATE SET project_id = excluded.project_id
                """,
                (project_id, machine_id, path),
            )

    def get_import_state(self, machine_id: str, source_path: str) -> ImportState | None:
        row = self.conn.execute(
            "SELECT * FROM import_state WHERE machine_id = ? AND source_path = ?",
            (machine_id, source_path),
        ).fetchone()
        if row is None:
            return None
        return ImportState(
            machine_id=row["machine_id"],
            source_path=row["source_path"],
            file_size=row["file_size"],
            file_mtime=row["file_mtime"],
            byte_offset=row["byte_offset"],
            last_imported_at=row["last_imported_at"],
        )

    def set_import_state(
        self,
        machine_id: str,
        source_path: str,
        *,
        file_size: int,
        file_mtime: float,
        byte_offset: int,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO import_state
                    (machine_id, source_path, file_size, file_mtime, byte_offset,
                     last_imported_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (machine_id, source_path) DO UPDATE SET
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    byte_offset = excluded.byte_offset,
                    last_imported_at = excluded.last_imported_at
                """,
                (machine_id, source_path, file_size, file_mtime, byte_offset, _utcnow()),
            )

    def reset_import_state(self, machine_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM import_state WHERE machine_id = ?", (machine_id,))

    # --- sessions and their streams ---

    _SESSION_FIELDS = frozenset(
        {
            "source_path",
            "cwd",
            "git_branch",
            "cli_version",
            "originator",
            "title",
            "model",
            "context_window",
            "task_duration_ms",
            "time_to_first_token_ms",
            "rate_limit_primary_used_percent",
            "rate_limit_secondary_used_percent",
            "effort",
            "approval_policy",
            "sandbox_policy_type",
            "collaboration_mode",
            "parent_session_id",
            "rate_limit_plan",
            "developer_instructions_hash",
            "user_instructions_hash",
            "cache_creation_5m_tokens",
            "cache_creation_1h_tokens",
            "usage_iteration_count",
            "entrypoint",
            "web_search_requests",
            "web_fetch_requests",
            "hook_count",
            "hook_error_count",
            "hook_commands",
            "hook_has_output_count",
        }
    )

    def ensure_session(self, session_id: str, machine_id: str, agent: str) -> bool:
        """Insert the session shell if missing; returns True when newly created."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, machine_id, agent) VALUES (?, ?, ?)",
                (session_id, machine_id, agent),
            )
        return cursor.rowcount == 1

    def set_session_fields(
        self, session_id: str, fields: Mapping[str, str | int | float | None]
    ) -> None:
        updates = {k: v for k, v in fields.items() if k in self._SESSION_FIELDS and v is not None}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.conn:
            self.conn.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = ?",  # noqa: S608
                (*updates.values(), session_id),
            )

    def set_session_project(self, session_id: str, project_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET project_id = ? WHERE session_id = ?",
                (project_id, session_id),
            )

    def delete_stream(
        self, session_id: str, stream_id: str, *, drop_subagent_runs: bool = False
    ) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND stream_id = ?",
                (session_id, stream_id),
            )
            self.conn.execute(
                "DELETE FROM tool_calls WHERE session_id = ? AND stream_id = ?",
                (session_id, stream_id),
            )
            self.conn.execute(
                "DELETE FROM context_events WHERE session_id = ? AND stream_id = ?",
                (session_id, stream_id),
            )
            if drop_subagent_runs:
                self.conn.execute("DELETE FROM subagent_runs WHERE session_id = ?", (session_id,))

    def next_idx(self, session_id: str, stream_id: str, table: str) -> int:
        if table not in ("messages", "tool_calls", "context_events"):
            raise ValueError(f"unexpected table: {table}")
        row = self.conn.execute(
            f"SELECT MAX(idx) FROM {table} WHERE session_id = ? AND stream_id = ?",  # noqa: S608
            (session_id, stream_id),
        ).fetchone()
        return 0 if row[0] is None else row[0] + 1

    def context_resume(self, session_id: str, stream_id: str) -> tuple[int, int, int, str | None]:
        """Return next event/call indexes, generation and the last event type."""
        row = self.conn.execute(
            """
            SELECT COALESCE(MAX(idx), -1) + 1 AS next_idx,
                   COALESCE(
                       MAX(CASE WHEN event_type = 'model_call' THEN model_call_index END),
                       -1
                   ) + 1 AS next_call,
                   COALESCE(MAX(compaction_generation), 0) AS generation
            FROM context_events WHERE session_id = ? AND stream_id = ?
            """,
            (session_id, stream_id),
        ).fetchone()
        last = self.conn.execute(
            """
            SELECT event_type,
                   json_extract(extra, '$.payload') AS has_payload
            FROM context_events
            WHERE session_id = ? AND stream_id = ?
              AND event_type IN ('compact_boundary', 'compact_marker')
            ORDER BY idx DESC LIMIT 1
            """,
            (session_id, stream_id),
        ).fetchone()
        previous_event = last["event_type"] if last is not None else None
        has_payload = last["has_payload"] if last is not None else None
        if previous_event == "compact_boundary" and has_payload == 1:
            previous_event = "compact_payload_boundary"
        return (
            row["next_idx"],
            row["next_call"],
            row["generation"],
            previous_event,
        )

    def insert_messages(
        self, session_id: str, machine_id: str, stream_id: str, records: list[MessageRec]
    ) -> None:
        if not records:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO messages
                    (session_id, machine_id, stream_id, idx, message_id, role, ts, model,
                     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                     reasoning_tokens, stop_reason, content_preview, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        machine_id,
                        stream_id,
                        r.idx,
                        r.message_id,
                        r.role,
                        r.ts,
                        r.model,
                        r.input_tokens,
                        r.output_tokens,
                        r.cache_read_tokens,
                        r.cache_creation_tokens,
                        r.reasoning_tokens,
                        r.stop_reason,
                        r.content_preview,
                        json.dumps(r.extra, ensure_ascii=False) if r.extra else None,
                    )
                    for r in records
                ],
            )

    def insert_context_events(
        self,
        session_id: str,
        machine_id: str,
        stream_id: str,
        records: list[ContextEventRec],
    ) -> None:
        if not records:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO context_events
                    (session_id, machine_id, stream_id, idx, ts, turn_id,
                     model_call_index, event_type, role, category, char_count,
                     byte_count, fingerprint, tool_name, command_kind,
                     target_fingerprint, input_tokens, output_tokens,
                     cache_read_tokens, cache_creation_tokens, reasoning_tokens,
                     context_window, compaction_generation, coverage, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        machine_id,
                        stream_id,
                        r.idx,
                        r.ts,
                        r.turn_id,
                        r.model_call_index,
                        r.event_type,
                        r.role,
                        r.category,
                        r.char_count,
                        r.byte_count,
                        r.fingerprint,
                        r.tool_name,
                        r.command_kind,
                        r.target_fingerprint,
                        r.input_tokens,
                        r.output_tokens,
                        r.cache_read_tokens,
                        r.cache_creation_tokens,
                        r.reasoning_tokens,
                        r.context_window,
                        r.compaction_generation,
                        r.coverage,
                        json.dumps(r.extra, ensure_ascii=False) if r.extra else None,
                    )
                    for r in records
                ],
            )

    def insert_tool_calls(
        self, session_id: str, machine_id: str, stream_id: str, records: list[ToolCallRec]
    ) -> None:
        if not records:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO tool_calls
                    (session_id, machine_id, stream_id, idx, call_id, tool_name, category,
                     target, ts, duration_ms, exit_code, is_error, command_kind,
                     stdout_bytes, stderr_bytes, output_bytes, patch_success,
                     changed_file_count, lines_added, lines_removed, input_preview,
                     output_preview, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        machine_id,
                        stream_id,
                        r.idx,
                        r.call_id,
                        r.tool_name,
                        r.category,
                        r.target,
                        r.ts,
                        r.duration_ms,
                        r.exit_code,
                        int(r.is_error),
                        r.command_kind,
                        r.stdout_bytes,
                        r.stderr_bytes,
                        r.output_bytes,
                        None if r.patch_success is None else int(r.patch_success),
                        r.changed_file_count,
                        r.lines_added,
                        r.lines_removed,
                        r.input_preview,
                        r.output_preview,
                        json.dumps(r.extra, ensure_ascii=False) if r.extra else None,
                    )
                    for r in records
                ],
            )

    def apply_tool_result(self, session_id: str, result: ToolResultRec) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE tool_calls SET
                    is_error = COALESCE(?, is_error),
                    -- exit_code/duration_ms are first-writer-wins: exec telemetry is
                    -- precise and arrives before the coarser output-header values.
                    exit_code = COALESCE(exit_code, ?),
                    -- Otherwise derive latency from the call ts -> result ts delta
                    -- (Claude reports no execution time).
                    duration_ms = COALESCE(duration_ms, ?, CAST(ROUND(
                        (julianday(?) - julianday(ts)) * 86400000) AS INTEGER)),
                    category = COALESCE(?, category),
                    command_kind = COALESCE(?, command_kind),
                    stdout_bytes = COALESCE(?, stdout_bytes),
                    stderr_bytes = COALESCE(?, stderr_bytes),
                    -- first-writer: exec aggregated_output size beats the
                    -- model-visible output (which prepends the header).
                    output_bytes = COALESCE(output_bytes, ?),
                    patch_success = COALESCE(?, patch_success),
                    changed_file_count = COALESCE(?, changed_file_count),
                    lines_added = COALESCE(?, lines_added),
                    lines_removed = COALESCE(?, lines_removed),
                    -- first writer wins: exec telemetry arrives before the model-visible
                    -- result and carries the richer aggregated output
                    output_preview = COALESCE(output_preview, ?),
                    extra = CASE
                        WHEN ? IS NULL THEN extra
                        WHEN extra IS NULL THEN ?
                        ELSE json_patch(extra, ?)
                    END
                WHERE session_id = ? AND call_id = ?
                """,
                (
                    None if result.is_error is None else int(result.is_error),
                    result.exit_code,
                    result.duration_ms,
                    result.ts,
                    result.category,
                    result.command_kind,
                    result.stdout_bytes,
                    result.stderr_bytes,
                    result.output_bytes,
                    None if result.patch_success is None else int(result.patch_success),
                    result.changed_file_count,
                    result.lines_added,
                    result.lines_removed,
                    result.output_preview,
                    json.dumps(result.extra, ensure_ascii=False) if result.extra else None,
                    json.dumps(result.extra, ensure_ascii=False) if result.extra else None,
                    json.dumps(result.extra, ensure_ascii=False) if result.extra else None,
                    session_id,
                    result.call_id,
                ),
            )

    def upsert_subagent_run(self, session_id: str, machine_id: str, rec: SubagentRec) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO subagent_runs
                    (agent_run_id, session_id, machine_id, agent_type, prompt, status,
                     total_duration_ms, total_tokens, total_tool_use_count, read_count,
                     search_count, bash_count, edit_file_count, lines_added, lines_removed,
                     extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.agent_run_id,
                    session_id,
                    machine_id,
                    rec.agent_type,
                    rec.prompt,
                    rec.status,
                    rec.total_duration_ms,
                    rec.total_tokens,
                    rec.total_tool_use_count,
                    rec.read_count,
                    rec.search_count,
                    rec.bash_count,
                    rec.edit_file_count,
                    rec.lines_added,
                    rec.lines_removed,
                    json.dumps(rec.extra, ensure_ascii=False) if rec.extra else None,
                ),
            )

    def sync_codex_subagent_runs(self, machine_id: str) -> int:
        """Mirror Codex subagent child sessions into subagent_runs on their parents.

        Codex writes each subagent thread as a standalone rollout file whose
        session_meta carries parent_thread_id. Idempotent: REPLACE per child.
        Returns the number of runs synced.
        """
        from wdi.ingest.models import SubagentRec

        children = self.conn.execute(
            """
            SELECT session_id, parent_session_id, entrypoint, duration_ms,
                   input_tokens, output_tokens, cache_read_tokens, tool_call_count
            FROM sessions
            WHERE agent = 'codex' AND machine_id = ? AND parent_session_id IS NOT NULL
            """,
            (machine_id,),
        ).fetchall()
        for child in children:
            # The parent rollout may not be imported (yet); keep the FK valid.
            self.ensure_session(child["parent_session_id"], machine_id, "codex")
            counts = self.conn.execute(
                """
                SELECT
                    SUM(category = 'read') AS read_count,
                    SUM(category = 'search') AS search_count,
                    SUM(category = 'exec') AS bash_count,
                    SUM(category = 'edit') AS edit_count,
                    SUM(lines_added) AS lines_added,
                    SUM(lines_removed) AS lines_removed
                FROM tool_calls
                WHERE session_id = ? AND stream_id = 'main'
                """,
                (child["session_id"],),
            ).fetchone()
            entrypoint = child["entrypoint"] or ""
            agent_type = (
                entrypoint.split(":", 1)[1] if entrypoint.startswith("subagent:") else "subagent"
            )
            self.upsert_subagent_run(
                child["parent_session_id"],
                machine_id,
                SubagentRec(
                    agent_run_id=child["session_id"],
                    agent_type=agent_type,
                    total_duration_ms=child["duration_ms"],
                    total_tokens=(child["input_tokens"] or 0)
                    + (child["output_tokens"] or 0)
                    + (child["cache_read_tokens"] or 0),
                    total_tool_use_count=child["tool_call_count"],
                    read_count=counts["read_count"],
                    search_count=counts["search_count"],
                    bash_count=counts["bash_count"],
                    edit_file_count=counts["edit_count"],
                    lines_added=counts["lines_added"],
                    lines_removed=counts["lines_removed"],
                ),
            )
        return len(children)

    def recompute_session_aggregates(self, session_id: str) -> None:
        """Recompute counters of the sessions row from its main stream rows.

        Idempotent by design: incremental appends just re-run the same SUMs.
        """
        with self.conn:
            self.conn.execute(
                """
                UPDATE sessions SET
                    user_msg_count = (
                        SELECT COUNT(*) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main' AND role = 'user'),
                    assistant_msg_count = (
                        SELECT COUNT(*) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main' AND role = 'assistant'),
                    tool_call_count = (
                        SELECT COUNT(*) FROM tool_calls
                        WHERE session_id = :sid AND stream_id = 'main'),
                    input_tokens = COALESCE((
                        SELECT SUM(input_tokens) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'), 0),
                    output_tokens = COALESCE((
                        SELECT SUM(output_tokens) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'), 0),
                    cache_read_tokens = COALESCE((
                        SELECT SUM(cache_read_tokens) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'), 0),
                    cache_creation_tokens = (
                        SELECT SUM(cache_creation_tokens) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'),
                    reasoning_tokens = (
                        SELECT SUM(reasoning_tokens) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'),
                    compaction_count = (
                        SELECT COUNT(*) FROM messages
                        WHERE session_id = :sid AND role = 'system'
                          AND json_extract(extra, '$.subtype') = 'compact_boundary'),
                    api_error_count = (
                        SELECT COUNT(*) FROM messages
                        WHERE session_id = :sid AND role = 'system'
                          AND json_extract(extra, '$.subtype') = 'api_error'),
                    -- Codex turns: started > complete when a turn was interrupted;
                    -- old rollouts have task_complete only.
                    turn_count = CASE WHEN agent = 'claude' THEN
                        (SELECT COUNT(*) FROM messages WHERE session_id = :sid AND stream_id =
                    'main' AND role = 'user')
                        ELSE MAX(
                        (SELECT COUNT(*) FROM messages
                         WHERE session_id = :sid AND stream_id = 'main'
                           AND json_extract(extra, '$.subtype') = 'task_started'),
                        (SELECT COUNT(*) FROM messages
                         WHERE session_id = :sid AND stream_id = 'main'
                           AND json_extract(extra, '$.subtype') = 'task_complete')) END,
                    turn_abort_count = (
                        SELECT COUNT(*) FROM messages
                        WHERE session_id = :sid
                          AND json_extract(extra, '$.subtype') = 'turn_aborted'),
                    task_duration_ms = (
                        SELECT SUM(json_extract(extra, '$.duration_ms')) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'
                          AND json_extract(extra, '$.subtype') = 'task_complete'),
                    time_to_first_token_ms = (
                        SELECT CAST(AVG(json_extract(extra, '$.ttft_ms')) AS INTEGER)
                        FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'
                          AND json_extract(extra, '$.subtype') = 'task_complete'),
                    -- Peak context a single turn saw: Claude assistant usage rows,
                    -- Codex token_count event rows.
                    peak_context_tokens = (
                        SELECT MAX(COALESCE(input_tokens, 0) + CASE WHEN sessions.agent =
                    'codex' THEN 0 ELSE COALESCE(cache_read_tokens, 0)
                                   + COALESCE(cache_creation_tokens, 0) END)
                        FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'
                          AND (input_tokens IS NOT NULL OR cache_read_tokens IS NOT NULL
                               OR cache_creation_tokens IS NOT NULL)),
                    web_search_requests = CASE
                        WHEN agent = 'codex' THEN (
                            SELECT COUNT(*) FROM tool_calls
                            WHERE session_id = :sid AND stream_id = 'main'
                              AND category = 'web')
                        ELSE web_search_requests
                    END,
                    started_at = (
                        SELECT MIN(ts) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'),
                    ended_at = (
                        SELECT MAX(ts) FROM messages
                        WHERE session_id = :sid AND stream_id = 'main'),
                    model = (
                        SELECT model FROM messages
                        WHERE session_id = :sid AND stream_id = 'main' AND model IS NOT NULL
                        GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1),
                    -- Headless SDK one-shots (session observers etc.) and Codex
                    -- subagent threads are noise for efficiency aggregates;
                    -- interactive entrypoints (cli, claude-desktop) stay visible.
                    is_noise = CASE
                        WHEN entrypoint = 'sdk-cli' OR parent_session_id IS NOT NULL THEN 1
                        ELSE 0
                    END
                WHERE session_id = :sid
                """,
                {"sid": session_id},
            )
            self.conn.execute(
                """
                UPDATE sessions SET duration_ms = CAST(ROUND(
                    (julianday(ended_at) - julianday(started_at)) * 86400000) AS INTEGER)
                WHERE session_id = ? AND started_at IS NOT NULL AND ended_at IS NOT NULL
                """,
                (session_id,),
            )

    # --- LLM analysis cache ---

    def get_llm_analysis(
        self, session_id: str, analysis_type: str, prompt_version: str
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM llm_analyses
            WHERE session_id = ? AND analysis_type = ? AND prompt_version = ?
            """,
            (session_id, analysis_type, prompt_version),
        ).fetchone()

    def upsert_llm_analysis(
        self,
        session_id: str,
        machine_id: str,
        *,
        analysis_type: str,
        prompt_version: str,
        model: str,
        score: float | None,
        result: dict[str, object],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO llm_analyses
                    (session_id, machine_id, analysis_type, prompt_version, model,
                     created_at, score, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id, analysis_type, prompt_version) DO UPDATE SET
                    model = excluded.model,
                    created_at = excluded.created_at,
                    score = excluded.score,
                    result = excluded.result
                """,
                (
                    session_id,
                    machine_id,
                    analysis_type,
                    prompt_version,
                    model,
                    _utcnow(),
                    score,
                    json.dumps(result, ensure_ascii=False),
                ),
            )

    def get_coach_report(self, filter_hash: str, prompt_version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM coach_reports WHERE filter_hash = ? AND prompt_version = ?",
            (filter_hash, prompt_version),
        ).fetchone()

    def upsert_coach_report(
        self,
        filter_hash: str,
        *,
        prompt_version: str,
        model: str,
        result: dict[str, object],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO coach_reports
                    (filter_hash, prompt_version, model, created_at, result)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (filter_hash, prompt_version) DO UPDATE SET
                    model = excluded.model,
                    created_at = excluded.created_at,
                    result = excluded.result
                """,
                (
                    filter_hash,
                    prompt_version,
                    model,
                    _utcnow(),
                    json.dumps(result, ensure_ascii=False),
                ),
            )

    def sessions_for_analysis(
        self,
        analysis_type: str,
        prompt_version: str,
        where: str,
        params: list[object],
        limit: int,
        *,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        """Sessions matching the filter awaiting analysis of this kind.

        With ``force`` the cache check is dropped, so already-analyzed sessions are
        re-selected (the upsert then overwrites their stored result).
        """
        cache_clause = (
            ""
            if force
            else """
              AND NOT EXISTS (
                SELECT 1 FROM llm_analyses a
                WHERE a.session_id = s.session_id
                  AND a.analysis_type = ? AND a.prompt_version = ?
              )"""
        )
        cache_params: tuple[object, ...] = () if force else (analysis_type, prompt_version)
        return self.conn.execute(
            f"""
            SELECT s.session_id, s.machine_id, s.agent
            FROM sessions s
            WHERE {where}{cache_clause}
            ORDER BY s.started_at DESC
            LIMIT ?
            """,  # noqa: S608 - where and cache_clause are built from fixed fragments
            (*params, *cache_params, limit),
        ).fetchall()
