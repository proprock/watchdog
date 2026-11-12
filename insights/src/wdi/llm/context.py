"""Read queries that gather per-session context for LLM analyzers.

Prefers the original transcript (full fidelity, sieved by segment kind) when the
local source file is available; falls back to stored DB previews otherwise
(e.g. Codex sessions or snapshots imported from another machine).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from wdi.llm import transcript

# Per-field cap for the summary context so one huge message can't dominate.
_FIELD_MAX = 8_000


def _is_observed_tool_event(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<observed_from_primary_session>") and (
        "<what_happened>" in stripped or "<tool_name>" in stripped
    )


def _source_segments(conn: sqlite3.Connection, session_id: str) -> list[transcript.Segment] | None:
    """Full-fidelity segments from the source file, or None when unavailable."""
    row = conn.execute(
        "SELECT agent, source_path FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None or not row["source_path"]:
        return None
    path = Path(row["source_path"])
    if not path.is_file():
        return None
    return list(transcript.iter_segments(path))


def user_prompts(conn: sqlite3.Connection, session_id: str, limit: int = 40) -> list[str]:
    segments = _source_segments(conn, session_id)
    if segments is not None:
        raw_prompts = [s.text for s in segments if s.kind == transcript.USER_PROMPT]
        prompts = [p for p in raw_prompts if not _is_observed_tool_event(p)]
        if raw_prompts:
            return prompts[:limit]
    rows = conn.execute(
        """
        SELECT content_preview FROM messages
        WHERE session_id = ? AND stream_id = 'main' AND role = 'user'
          AND content_preview IS NOT NULL
        ORDER BY idx
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    return [r["content_preview"] for r in rows if not _is_observed_tool_event(r["content_preview"])]


def _last_assistant(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT content_preview FROM messages
        WHERE session_id = ? AND stream_id = 'main' AND role = 'assistant'
          AND content_preview IS NOT NULL
        ORDER BY idx DESC LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return row["content_preview"] if row else None


def _tool_counts(conn: sqlite3.Connection, session_id: str) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT category, COUNT(*) AS calls, COALESCE(SUM(is_error), 0) AS errors
        FROM tool_calls
        WHERE session_id = ? AND stream_id = 'main'
        GROUP BY category
        """,
        (session_id,),
    ).fetchall()
    return {r["category"]: {"calls": r["calls"], "errors": r["errors"]} for r in rows}


def summary_context(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        """
        SELECT agent, model, title, duration_ms, user_msg_count, assistant_msg_count,
               tool_call_count, compaction_count, api_error_count, input_tokens,
               output_tokens, cache_read_tokens, reasoning_tokens, context_window,
               time_to_first_token_ms, task_duration_ms
        FROM sessions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    segments = _source_segments(conn, session_id)
    if segments is not None:
        prompts = [s.text for s in segments if s.kind == transcript.USER_PROMPT]
        assistant_texts = [s.text for s in segments if s.kind == transcript.ASSISTANT_TEXT]
        first_prompt = prompts[0] if prompts else None
        final_message = assistant_texts[-1] if assistant_texts else None
        error_outputs = [
            s.text[:_FIELD_MAX] for s in segments if s.kind == transcript.TOOL_RESULT and s.is_error
        ]
    else:
        db_prompts = user_prompts(conn, session_id, limit=3)
        first_prompt = db_prompts[0] if db_prompts else None
        final_message = _last_assistant(conn, session_id)
        error_outputs = []

    data: dict[str, Any] = {
        "agent": row["agent"],
        "model": row["model"],
        "title": row["title"],
        "duration_ms": row["duration_ms"],
        "messages": {
            "user": row["user_msg_count"],
            "assistant": row["assistant_msg_count"],
        },
        "tool_call_count": row["tool_call_count"],
        "compactions": row["compaction_count"],
        "api_errors": row["api_error_count"],
        "efficiency": {
            "context_pressure": (
                (row["input_tokens"] + row["cache_read_tokens"]) / row["context_window"]
                if row["context_window"]
                else None
            ),
            "time_to_first_token_ms": row["time_to_first_token_ms"],
            "task_duration_ms": row["task_duration_ms"],
            "reasoning_ratio": (
                row["reasoning_tokens"] / (row["input_tokens"] + row["cache_read_tokens"])
                if row["reasoning_tokens"] is not None
                and (row["input_tokens"] + row["cache_read_tokens"])
                else None
            ),
        },
        "first_prompt": first_prompt[:_FIELD_MAX] if first_prompt else None,
        "final_message": final_message[:_FIELD_MAX] if final_message else None,
        "error_outputs": error_outputs[:10],
        "tools": _tool_counts(conn, session_id),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def workflow_recommendations_context(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        """
        SELECT s.agent, s.model, s.title, s.input_tokens, s.output_tokens,
               s.cache_read_tokens, s.reasoning_tokens, s.context_window,
               s.time_to_first_token_ms, s.compaction_count, s.api_error_count,
               COALESCE(tc.search_count, 0) AS search_count,
               COALESCE(tc.edit_count, 0) AS edit_count,
               COALESCE(tc.exec_errors, 0) AS exec_errors,
               COALESCE(rr.repeated_reads, 0) AS repeated_reads
        FROM sessions s
        LEFT JOIN (
            SELECT session_id,
                   SUM(category = 'search') AS search_count,
                   SUM(category = 'edit') AS edit_count,
                   SUM(category = 'exec' AND is_error) AS exec_errors
            FROM tool_calls WHERE stream_id = 'main'
            GROUP BY session_id
        ) tc ON tc.session_id = s.session_id
        LEFT JOIN (
            SELECT session_id, SUM(c - 1) AS repeated_reads FROM (
                SELECT session_id, target, COUNT(*) AS c FROM tool_calls
                WHERE stream_id = 'main' AND category = 'read' AND target IS NOT NULL
                GROUP BY session_id, target HAVING COUNT(*) > 1
            ) GROUP BY session_id
        ) rr ON rr.session_id = s.session_id
        WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()
    prompts = user_prompts(conn, session_id, limit=5)
    data = {
        "session": {
            "agent": row["agent"],
            "model": row["model"],
            "title": row["title"],
            "tokens": {
                "input": row["input_tokens"],
                "output": row["output_tokens"],
                "cache_read": row["cache_read_tokens"],
                "reasoning": row["reasoning_tokens"],
            },
            "context_pressure": (
                (row["input_tokens"] + row["cache_read_tokens"]) / row["context_window"]
                if row["context_window"]
                else None
            ),
            "time_to_first_token_ms": row["time_to_first_token_ms"],
            "compactions": row["compaction_count"],
            "api_errors": row["api_error_count"],
        },
        "developer_prompt_sample": prompts,
        "tooling_signals": {
            "search_count": row["search_count"],
            "edit_count": row["edit_count"],
            "repeated_reads": row["repeated_reads"],
            "exec_errors": row["exec_errors"],
        },
        "advice_targets": [
            "developer prompting and task decomposition",
            "agent tool selection, search/read reuse, and subagent prompting",
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def agent_efficiency_context(conn: sqlite3.Connection, session_id: str) -> str:
    runs = conn.execute(
        """
        SELECT agent_run_id, agent_type, status, total_tokens, total_tool_use_count,
               read_count, search_count, edit_file_count
        FROM subagent_runs WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    run_data: list[dict[str, Any]] = []
    for run in runs:
        traces = conn.execute(
            """
            SELECT category, COUNT(*) AS calls, COALESCE(SUM(is_error), 0) AS errors
            FROM tool_calls
            WHERE session_id = ? AND stream_id = ?
            GROUP BY category
            """,
            (session_id, run["agent_run_id"]),
        ).fetchall()
        repeated = conn.execute(
            """
            SELECT COALESCE(SUM(c - 1), 0) AS redundant FROM (
                SELECT COUNT(*) AS c FROM tool_calls
                WHERE session_id = ? AND stream_id = ? AND category = 'read'
                  AND target IS NOT NULL
                GROUP BY target HAVING COUNT(*) > 1
            )
            """,
            (session_id, run["agent_run_id"]),
        ).fetchone()
        run_data.append(
            {
                "agent_type": run["agent_type"],
                "status": run["status"],
                "total_tokens": run["total_tokens"],
                "total_tool_use_count": run["total_tool_use_count"],
                "read_count": run["read_count"],
                "search_count": run["search_count"],
                "edit_file_count": run["edit_file_count"],
                "redundant_reads": repeated["redundant"],
                "trace_tools": {
                    t["category"]: {"calls": t["calls"], "errors": t["errors"]} for t in traces
                },
            }
        )
    return json.dumps({"subagent_runs": run_data}, ensure_ascii=False, indent=2)


def has_subagents(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM subagent_runs WHERE session_id = ? LIMIT 1", (session_id,)
    ).fetchone()
    return row is not None
