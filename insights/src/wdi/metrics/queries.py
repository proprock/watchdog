"""Read-only metric queries. All SQL for the metrics layer lives here."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from wdi.metrics.filters import SessionFilter


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


# Earliest user prompt of the main stream; fallback label when a session has no title.
_FIRST_PROMPT_SQL = """
    (SELECT m.content_preview FROM messages m
     WHERE m.session_id = s.session_id AND m.stream_id = 'main' AND m.role = 'user'
       AND m.content_preview IS NOT NULL
     ORDER BY m.idx LIMIT 1) AS first_prompt
"""


def overview(conn: sqlite3.Connection, flt: SessionFilter) -> sqlite3.Row:
    where, params = flt.where()
    return conn.execute(
        f"""
        WITH filtered AS (
            SELECT * FROM sessions s WHERE {where}
        ),
        repeated AS (
            SELECT SUM(c - 1) AS repeated_reads FROM (
                SELECT t.session_id, t.target, COUNT(*) AS c
                FROM tool_calls t
                JOIN filtered s ON s.session_id = t.session_id
                WHERE t.stream_id = 'main' AND t.category = 'read' AND t.target IS NOT NULL
                GROUP BY t.session_id, t.target HAVING COUNT(*) > 1
            )
        ),
        edits AS (
            SELECT COUNT(*) AS edit_calls
            FROM tool_calls t
            JOIN filtered s ON s.session_id = t.session_id
            WHERE t.stream_id = 'main' AND t.category = 'edit'
        )
        SELECT
            COUNT(*) AS sessions,
            SUM(agent = 'claude') AS claude_sessions,
            SUM(agent = 'codex') AS codex_sessions,
            COUNT(DISTINCT project_id) AS projects,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
            COALESCE(SUM(tool_call_count), 0) AS tool_calls,
            COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
            COALESCE(SUM(compaction_count), 0) AS compactions,
            COALESCE(SUM(api_error_count), 0) AS api_errors,
            AVG(time_to_first_token_ms) AS avg_time_to_first_token_ms,
            MAX(CASE
                WHEN context_window > 0 AND peak_context_tokens IS NOT NULL
                THEN CAST(peak_context_tokens AS REAL) / context_window
            END) AS max_context_pressure,
            COALESCE(SUM(turn_count), 0) AS turns,
            COALESCE(SUM(turn_abort_count), 0) AS turn_aborts,
            MAX(rate_limit_primary_used_percent) AS rate_limit_primary_max,
            MAX(rate_limit_secondary_used_percent) AS rate_limit_secondary_max,
            COALESCE((SELECT edit_calls FROM edits), 0) AS edit_calls,
            COALESCE((SELECT repeated_reads FROM repeated), 0) AS repeated_reads
        FROM filtered
        """,  # noqa: S608 - where is built from parameterized fragments
        tuple(params),
    ).fetchone()


def tokens_by_model(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            COALESCE(model, '(unknown)') AS model,
            COUNT(*) AS sessions,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM sessions s
        WHERE {where}
        GROUP BY model
        ORDER BY input_tokens + output_tokens DESC
        """,  # noqa: S608
        params,
    )


def activity_by_period(
    conn: sqlite3.Connection, flt: SessionFilter, period: str = "day"
) -> list[sqlite3.Row]:
    if period not in ("day", "week"):
        raise ValueError(f"unexpected period: {period}")
    bucket = "substr(started_at, 1, 10)" if period == "day" else "strftime('%Y-W%W', started_at)"
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            {bucket} AS period,
            COUNT(*) AS sessions,
            SUM(agent = 'claude') AS claude_sessions,
            SUM(agent = 'codex') AS codex_sessions,
            COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
        FROM sessions s
        WHERE {where} AND started_at IS NOT NULL
        GROUP BY period
        ORDER BY period
        """,  # noqa: S608
        params,
    )


def tool_categories(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            t.category AS category,
            COUNT(*) AS calls,
            COALESCE(SUM(t.is_error), 0) AS errors
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main'
        GROUP BY t.category
        ORDER BY calls DESC
        """,  # noqa: S608
        params,
    )


def top_failing_tools(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 10
) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            t.tool_name AS tool_name,
            COUNT(*) AS calls,
            COALESCE(SUM(t.is_error), 0) AS errors
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main'
        GROUP BY t.tool_name
        HAVING errors > 0
        ORDER BY errors DESC, calls DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def shell_failure_clusters(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 10
) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            COALESCE(t.command_kind, t.tool_name) AS command_kind,
            COUNT(*) AS calls,
            COALESCE(SUM(t.is_error), 0) AS errors
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main' AND t.category = 'exec'
        GROUP BY command_kind
        HAVING errors > 0
        ORDER BY errors DESC, calls DESC, command_kind
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def projects(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            COALESCE(p.canonical_name, '(none)') AS project,
            COUNT(*) AS sessions,
            COALESCE(SUM(s.input_tokens + s.output_tokens), 0) AS tokens,
            COALESCE(SUM(s.tool_call_count), 0) AS tool_calls
        FROM sessions s
        LEFT JOIN projects p ON p.project_id = s.project_id
        WHERE {where}
        GROUP BY project
        ORDER BY sessions DESC
        """,  # noqa: S608
        params,
    )


def subagent_types(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    """Per agent_type aggregates plus trace-derived efficiency signals.

    Trace signals join the subagent step rows (tool_calls.stream_id =
    subagent_runs.agent_run_id) to count failed calls and repeated file reads.
    """
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        WITH runs AS (
            SELECT sr.agent_run_id, sr.agent_type, sr.total_tokens,
                   sr.total_duration_ms, sr.total_tool_use_count, sr.read_count,
                   sr.search_count, sr.edit_file_count
            FROM subagent_runs sr
            JOIN sessions s ON s.session_id = sr.session_id
            WHERE {where}
        ),
        repeated AS (
            -- redundant re-reads of the same target within one run (COUNT - 1)
            SELECT t.stream_id, COUNT(*) - 1 AS repeated_reads
            FROM tool_calls t
            JOIN runs r ON r.agent_run_id = t.stream_id
            WHERE t.category = 'read' AND t.target IS NOT NULL
            GROUP BY t.stream_id, t.target
            HAVING COUNT(*) > 1
        ),
        trace_errors AS (
            SELECT t.stream_id, SUM(t.is_error) AS errors, COUNT(*) AS calls
            FROM tool_calls t
            JOIN runs r ON r.agent_run_id = t.stream_id
            GROUP BY t.stream_id
        )
        SELECT
            COALESCE(r.agent_type, '(unknown)') AS agent_type,
            COUNT(*) AS runs,
            AVG(r.total_tokens) AS avg_tokens,
            COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
            AVG(r.total_duration_ms) AS avg_duration_ms,
            AVG(r.total_tool_use_count) AS avg_tool_calls,
            COALESCE(SUM(r.read_count), 0) AS read_count,
            COALESCE(SUM(r.search_count), 0) AS search_count,
            COALESCE(SUM(r.edit_file_count), 0) AS edit_count,
            COALESCE(SUM(re.repeated_reads), 0) AS repeated_reads,
            COALESCE(SUM(te.errors), 0) AS trace_errors,
            COALESCE(SUM(te.calls), 0) AS trace_calls
        FROM runs r
        LEFT JOIN (
            SELECT stream_id, SUM(repeated_reads) AS repeated_reads
            FROM repeated GROUP BY stream_id
        ) re ON re.stream_id = r.agent_run_id
        LEFT JOIN trace_errors te ON te.stream_id = r.agent_run_id
        GROUP BY r.agent_type
        ORDER BY runs DESC
        """,  # noqa: S608
        params,
    )


def cache_trend(
    conn: sqlite3.Connection, flt: SessionFilter, period: str = "day"
) -> list[sqlite3.Row]:
    """Per-period cache economics: fresh input vs cache reads vs cache writes."""
    if period not in ("day", "week"):
        raise ValueError(f"unexpected period: {period}")
    bucket = "substr(started_at, 1, 10)" if period == "day" else "strftime('%Y-W%W', started_at)"
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            {bucket} AS period,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
        FROM sessions s
        WHERE {where} AND started_at IS NOT NULL
        GROUP BY period
        ORDER BY period
        """,  # noqa: S608
        params,
    )


def waste_summary(conn: sqlite3.Connection, flt: SessionFilter) -> sqlite3.Row:
    """Context-waste indicators across the filtered sessions (main streams)."""
    where, params = flt.where()
    return conn.execute(
        f"""
        WITH filtered AS (
            SELECT session_id, agent, web_search_requests, web_fetch_requests,
                   turn_abort_count
            FROM sessions s WHERE {where}
        ),
        repeated AS (
            SELECT SUM(c - 1) AS repeated_reads FROM (
                SELECT COUNT(*) AS c
                FROM tool_calls t
                JOIN filtered s ON s.session_id = t.session_id
                WHERE t.stream_id = 'main' AND t.category = 'read' AND t.target IS NOT NULL
                GROUP BY t.session_id, t.target HAVING COUNT(*) > 1
            )
        ),
        duplicates AS (
            SELECT SUM(c - 1) AS duplicate_commands FROM (
                SELECT COUNT(*) AS c
                FROM tool_calls t
                JOIN filtered s ON s.session_id = t.session_id
                WHERE t.stream_id = 'main' AND t.category = 'exec' AND t.target IS NOT NULL
                GROUP BY t.session_id, t.target HAVING COUNT(*) > 1
            )
        ),
        failed AS (
            SELECT COUNT(*) AS failed_calls,
                   COALESCE(SUM(t.output_bytes), 0) AS failed_output_bytes
            FROM tool_calls t
            JOIN filtered s ON s.session_id = t.session_id
            WHERE t.stream_id = 'main' AND t.is_error = 1
        ),
        truncated AS (
            -- stop_reason is a Claude concept; gate to keep semantics explicit.
            SELECT COUNT(*) AS truncated_outputs
            FROM messages m
            JOIN filtered s ON s.session_id = m.session_id
            WHERE m.stream_id = 'main' AND m.stop_reason = 'max_tokens'
              AND s.agent = 'claude'
        )
        SELECT
            COALESCE((SELECT repeated_reads FROM repeated), 0) AS repeated_reads,
            COALESCE((SELECT duplicate_commands FROM duplicates), 0) AS duplicate_commands,
            (SELECT failed_calls FROM failed) AS failed_calls,
            (SELECT failed_output_bytes FROM failed) AS failed_output_bytes,
            (SELECT truncated_outputs FROM truncated) AS truncated_outputs,
            COALESCE(SUM(web_search_requests), 0) AS web_search_requests,
            COALESCE(SUM(web_fetch_requests), 0) AS web_fetch_requests,
            COALESCE(SUM(turn_abort_count), 0) AS aborted_turns
        FROM filtered
        """,  # noqa: S608
        tuple(params),
    ).fetchone()


def top_repeated_targets(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 10
) -> list[sqlite3.Row]:
    """Files re-read the most within single sessions (main streams)."""
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT target, SUM(c) AS reads, SUM(c - 1) AS wasted_reads,
               COUNT(DISTINCT session_id) AS sessions
        FROM (
            SELECT t.session_id, t.target, COUNT(*) AS c
            FROM tool_calls t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE {where} AND t.stream_id = 'main'
              AND t.category = 'read' AND t.target IS NOT NULL
            GROUP BY t.session_id, t.target HAVING COUNT(*) > 1
        )
        GROUP BY target
        ORDER BY wasted_reads DESC, reads DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def tool_detail(conn: sqlite3.Connection, flt: SessionFilter, limit: int = 15) -> list[sqlite3.Row]:
    """Per-tool call stats: volume, errors, latency, context cost (main streams)."""
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            t.tool_name AS tool_name,
            COUNT(*) AS calls,
            COALESCE(SUM(t.is_error), 0) AS errors,
            AVG(t.duration_ms) AS avg_duration_ms,
            COALESCE(SUM(t.output_bytes), 0) AS output_bytes
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main'
        GROUP BY t.tool_name
        ORDER BY calls DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def tool_durations(
    conn: sqlite3.Connection, flt: SessionFilter, tool_names: Sequence[str]
) -> list[sqlite3.Row]:
    """Raw durations for the given tools; percentiles are computed in Python."""
    if not tool_names:
        return []
    where, params = flt.where()
    placeholders = ", ".join("?" for _ in tool_names)
    return _rows(
        conn,
        f"""
        SELECT t.tool_name AS tool_name, t.duration_ms AS duration_ms
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main'
          AND t.duration_ms IS NOT NULL AND t.tool_name IN ({placeholders})
        """,  # noqa: S608
        [*params, *tool_names],
    )


def hook_summary(conn: sqlite3.Connection, flt: SessionFilter) -> sqlite3.Row:
    """Aggregate hook activity across filtered sessions (hooks are Claude-only)."""
    where, params = flt.where()
    return conn.execute(
        f"""
        SELECT
            SUM(hook_count > 0) AS sessions_with_hooks,
            COALESCE(SUM(hook_count), 0) AS hook_count,
            COALESCE(SUM(hook_error_count), 0) AS hook_error_count,
            COALESCE(SUM(hook_has_output_count), 0) AS hook_has_output_count
        FROM sessions s
        WHERE {where} AND s.agent = 'claude'
        """,  # noqa: S608
        tuple(params),
    ).fetchone()


def availability(conn: sqlite3.Connection, flt: SessionFilter) -> sqlite3.Row:
    """Which agent-specific data exists under the filter; drives conditional display."""
    where, params = flt.where()
    return conn.execute(
        f"""
        WITH filtered AS (
            SELECT * FROM sessions s WHERE {where}
        )
        SELECT
            SUM(agent = 'claude') AS claude_sessions,
            SUM(agent = 'codex') AS codex_sessions,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(hook_count), 0) AS hook_count,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
            COUNT(time_to_first_token_ms) AS ttft_sessions,
            COUNT(context_window) AS context_window_sessions,
            COUNT(rate_limit_primary_used_percent) AS rate_limit_sessions,
            COALESCE(SUM(web_search_requests), 0)
                + COALESCE(SUM(web_fetch_requests), 0) AS web_requests,
            COALESCE(SUM(turn_abort_count), 0) AS turn_aborts,
            EXISTS(
                SELECT 1 FROM messages m
                WHERE m.stop_reason IS NOT NULL
                  AND m.session_id IN (SELECT session_id FROM filtered)
            ) AS has_stop_reason,
            EXISTS(
                SELECT 1 FROM tool_calls t
                WHERE t.category = 'mcp'
                  AND t.session_id IN (SELECT session_id FROM filtered)
            ) AS has_mcp,
            EXISTS(
                SELECT 1 FROM subagent_runs r
                WHERE r.session_id IN (SELECT session_id FROM filtered)
            ) AS has_subagents
        FROM filtered
        """,  # noqa: S608
        tuple(params),
    ).fetchone()


def project_names(conn: sqlite3.Connection) -> list[str]:
    """All known project names for filter dropdowns (intentionally unfiltered)."""
    rows = conn.execute("SELECT canonical_name FROM projects ORDER BY canonical_name").fetchall()
    return [row["canonical_name"] for row in rows]


def hook_commands(conn: sqlite3.Connection, flt: SessionFilter) -> list[str]:
    """Newline-joined hook command lists of filtered sessions (split in Python)."""
    where, params = flt.where()
    rows = _rows(
        conn,
        f"""
        SELECT hook_commands FROM sessions s
        WHERE {where} AND hook_commands IS NOT NULL
        """,  # noqa: S608
        params,
    )
    return [row["hook_commands"] for row in rows]


def assistant_usage_rows(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    """Per-assistant-message usage of Claude main streams, ordered within session.

    Raw input for cache-miss attribution; the pairwise classification runs in
    Python (metrics.compute) because it needs lookbehind and timestamp math.
    """
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT m.session_id, m.idx, m.ts, m.model, m.input_tokens,
               m.cache_read_tokens, m.cache_creation_tokens
        FROM messages m
        JOIN sessions s ON s.session_id = m.session_id
        WHERE {where} AND s.agent = 'claude' AND m.stream_id = 'main'
          AND m.role = 'assistant' AND m.ts IS NOT NULL
        ORDER BY m.session_id, m.idx
        """,  # noqa: S608
        params,
    )


def tool_call_times(conn: sqlite3.Connection, flt: SessionFilter) -> list[sqlite3.Row]:
    """Tool call timestamps and durations of Claude main streams (for gap attribution)."""
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT t.session_id, t.ts, t.duration_ms
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND s.agent = 'claude' AND t.stream_id = 'main'
          AND t.ts IS NOT NULL AND t.duration_ms IS NOT NULL
        ORDER BY t.session_id, t.ts
        """,  # noqa: S608
        params,
    )


def tool_sessions(
    conn: sqlite3.Connection, flt: SessionFilter, tool_name: str, limit: int = 25
) -> list[sqlite3.Row]:
    """Sessions using one tool, ranked by its errors then call volume."""
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT s.session_id, s.title, s.agent, s.started_at,
               COUNT(*) AS calls,
               COALESCE(SUM(t.is_error), 0) AS errors,
               AVG(t.duration_ms) AS avg_duration_ms,
               {_FIRST_PROMPT_SQL}
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main' AND t.tool_name = ?
        GROUP BY s.session_id
        ORDER BY errors DESC, calls DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, tool_name, limit],
    )


def subagent_opportunities(
    conn: sqlite3.Connection, flt: SessionFilter, min_lookups: int = 20, limit: int = 10
) -> list[sqlite3.Row]:
    """Exploration-heavy sessions that used no subagents.

    Heavy reading/searching in the main context is the pattern subagents are
    meant to absorb; these sessions quantify the missed opportunity.
    """
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT s.session_id, s.title, s.agent,
               SUM(t.category = 'search') AS search_count,
               SUM(t.category = 'read') AS read_count,
               COALESCE(SUM(CASE WHEN t.category IN ('read', 'search')
                                 THEN t.output_bytes END), 0) AS lookup_output_bytes
        FROM tool_calls t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {where} AND t.stream_id = 'main'
          AND NOT EXISTS (
              SELECT 1 FROM subagent_runs sr WHERE sr.session_id = s.session_id
          )
        GROUP BY s.session_id
        HAVING search_count + read_count >= ?
        ORDER BY search_count + read_count DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, min_lookups, limit],
    )


def session_context_curve(
    conn: sqlite3.Connection, session_id: str, stream_id: str = "main"
) -> list[sqlite3.Row]:
    """Context size per turn plus compaction markers, ordered by idx.

    context_tokens approximates what the model saw: fresh input + cache read +
    cache written. Claude carries usage on assistant rows; Codex on token_count
    event rows. Compaction rows carry pre/post token counts from extra.
    """
    return conn.execute(
        """
        SELECT idx, ts, role,
               COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0)
                   + COALESCE(cache_creation_tokens, 0) AS context_tokens,
               output_tokens,
               json_extract(extra, '$.subtype') AS subtype,
               json_extract(extra, '$.pre_tokens') AS pre_tokens,
               json_extract(extra, '$.post_tokens') AS post_tokens
        FROM messages
        WHERE session_id = ? AND stream_id = ?
          AND ((role = 'assistant'
                AND (input_tokens IS NOT NULL OR cache_read_tokens IS NOT NULL))
               OR json_extract(extra, '$.subtype') IN ('compact_boundary', 'token_count'))
        ORDER BY idx
        """,
        (session_id, stream_id),
    ).fetchall()


def session_waste(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    """Waste indicators of one session's main stream."""
    return conn.execute(
        """
        WITH repeated AS (
            SELECT SUM(c - 1) AS repeated_reads FROM (
                SELECT COUNT(*) AS c FROM tool_calls
                WHERE session_id = :sid AND stream_id = 'main'
                  AND category = 'read' AND target IS NOT NULL
                GROUP BY target HAVING COUNT(*) > 1
            )
        ),
        duplicates AS (
            SELECT SUM(c - 1) AS duplicate_commands FROM (
                SELECT COUNT(*) AS c FROM tool_calls
                WHERE session_id = :sid AND stream_id = 'main'
                  AND category = 'exec' AND target IS NOT NULL
                GROUP BY target HAVING COUNT(*) > 1
            )
        )
        SELECT
            COALESCE((SELECT repeated_reads FROM repeated), 0) AS repeated_reads,
            COALESCE((SELECT duplicate_commands FROM duplicates), 0) AS duplicate_commands,
            (SELECT COUNT(*) FROM tool_calls
             WHERE session_id = :sid AND stream_id = 'main' AND is_error = 1) AS failed_calls,
            (SELECT COALESCE(SUM(output_bytes), 0) FROM tool_calls
             WHERE session_id = :sid AND stream_id = 'main' AND is_error = 1)
                AS failed_output_bytes,
            (SELECT COUNT(*) FROM messages
             WHERE session_id = :sid AND stream_id = 'main'
               AND stop_reason = 'max_tokens') AS truncated_outputs
        """,
        {"sid": session_id},
    ).fetchone()


def session_repeated_targets(
    conn: sqlite3.Connection, session_id: str, limit: int = 10
) -> list[sqlite3.Row]:
    """Files re-read within one session's main stream."""
    return conn.execute(
        """
        SELECT target, COUNT(*) AS reads
        FROM tool_calls
        WHERE session_id = ? AND stream_id = 'main'
          AND category = 'read' AND target IS NOT NULL
        GROUP BY target HAVING COUNT(*) > 1
        ORDER BY reads DESC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()


def oversearching_sessions(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 10
) -> list[sqlite3.Row]:
    """Sessions whose main-stream tool use looks search-heavy / edit-light.

    Signals: search vs edit call counts and redundant re-reads of the same target
    (COUNT - 1 per repeated target). Mirrors the trace logic in subagent_types but
    keyed on the main stream instead of subagent runs.
    """
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        WITH calls AS (
            SELECT t.session_id,
                   SUM(t.category = 'search') AS search_count,
                   SUM(t.category = 'read') AS read_count,
                   SUM(t.category = 'edit') AS edit_count
            FROM tool_calls t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE {where} AND t.stream_id = 'main'
            GROUP BY t.session_id
        ),
        repeated AS (
            SELECT t.session_id, SUM(c) AS repeated_reads
            FROM (
                SELECT t.session_id, COUNT(*) - 1 AS c
                FROM tool_calls t
                JOIN sessions s ON s.session_id = t.session_id
                WHERE {where} AND t.stream_id = 'main'
                  AND t.category = 'read' AND t.target IS NOT NULL
                GROUP BY t.session_id, t.target
                HAVING COUNT(*) > 1
            ) t
            GROUP BY t.session_id
        )
        SELECT
            s.session_id, s.title, s.agent,
            COALESCE(c.search_count, 0) AS search_count,
            COALESCE(c.read_count, 0) AS read_count,
            COALESCE(c.edit_count, 0) AS edit_count,
            COALESCE(re.repeated_reads, 0) AS repeated_reads
        FROM calls c
        JOIN sessions s ON s.session_id = c.session_id
        LEFT JOIN repeated re ON re.session_id = c.session_id
        WHERE c.search_count > 0 OR COALESCE(re.repeated_reads, 0) > 0
        ORDER BY search_count DESC, repeated_reads DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def prompt_quality_reports(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 20
) -> list[sqlite3.Row]:
    """Stored prompt-quality analyses (worst score first).

    Reads pre-computed rows from llm_analyses; sessions must be analyzed with
    `wdi analyze prompt-quality` first. The `result` column is a JSON string.
    """
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            s.session_id, s.title, s.agent,
            a.score, a.result, a.created_at, a.model
        FROM llm_analyses a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE {where} AND a.analysis_type = 'prompt_quality'
        ORDER BY a.score ASC, a.created_at DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def session_list(
    conn: sqlite3.Connection, flt: SessionFilter, limit: int = 20
) -> list[sqlite3.Row]:
    where, params = flt.where()
    return _rows(
        conn,
        f"""
        SELECT
            s.session_id, s.agent, s.title, s.model, s.started_at, s.duration_ms,
            s.input_tokens, s.output_tokens, s.tool_call_count,
            COALESCE(p.canonical_name, '(none)') AS project,
            {_FIRST_PROMPT_SQL},
            COALESCE(tc.calls, 0) AS calls,
            COALESCE(tc.errors, 0) AS errors,
            COALESCE(tc.search_count, 0) AS search_count,
            COALESCE(tc.edit_count, 0) AS edit_count,
            COALESCE(rr.repeated_reads, 0) AS repeated_reads,
            qa.score AS summary_score
        FROM sessions s
        LEFT JOIN projects p ON p.project_id = s.project_id
        LEFT JOIN (
            SELECT session_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(is_error), 0) AS errors,
                   SUM(category = 'search') AS search_count,
                   SUM(category = 'edit') AS edit_count
            FROM tool_calls WHERE stream_id = 'main'
            GROUP BY session_id
        ) tc ON tc.session_id = s.session_id
        LEFT JOIN (
            SELECT session_id, SUM(c - 1) AS repeated_reads FROM (
                SELECT session_id, COUNT(*) AS c FROM tool_calls
                WHERE stream_id = 'main' AND category = 'read' AND target IS NOT NULL
                GROUP BY session_id, target HAVING COUNT(*) > 1
            ) GROUP BY session_id
        ) rr ON rr.session_id = s.session_id
        LEFT JOIN (
            SELECT session_id, score FROM (
                SELECT session_id, score,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id ORDER BY created_at DESC
                       ) AS rn
                FROM llm_analyses WHERE analysis_type = 'session_summary'
            ) WHERE rn = 1
        ) qa ON qa.session_id = s.session_id
        WHERE {where}
        ORDER BY s.started_at DESC
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    )


def session_detail(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT s.*, COALESCE(p.canonical_name, '(none)') AS project,
            {_FIRST_PROMPT_SQL}
        FROM sessions s
        LEFT JOIN projects p ON p.project_id = s.project_id
        WHERE s.session_id = ?
        """,  # noqa: S608
        (session_id,),
    ).fetchone()


def session_tool_categories(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT category, COUNT(*) AS calls, COALESCE(SUM(is_error), 0) AS errors
        FROM tool_calls
        WHERE session_id = ? AND stream_id = 'main'
        GROUP BY category
        ORDER BY calls DESC
        """,
        (session_id,),
    ).fetchall()


def session_analyses(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Latest stored LLM analysis per analysis_type for one session.

    Keeps only the newest prompt_version per type. `result` is a JSON string.
    """
    return conn.execute(
        """
        SELECT a.analysis_type, a.score, a.result, a.model, a.created_at, a.prompt_version
        FROM llm_analyses a
        WHERE a.session_id = ?
          AND a.created_at = (
              SELECT MAX(a2.created_at) FROM llm_analyses a2
              WHERE a2.session_id = a.session_id
                AND a2.analysis_type = a.analysis_type
          )
        ORDER BY a.analysis_type
        """,
        (session_id,),
    ).fetchall()


def session_subagents(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Per-run subagent stats plus trace-derived efficiency signals for badging."""
    return conn.execute(
        """
        SELECT sr.agent_run_id, sr.agent_type, sr.status, sr.total_tokens,
               sr.total_duration_ms, sr.total_tool_use_count, sr.search_count,
               sr.edit_file_count,
               COALESCE(te.errors, 0) AS trace_errors,
               COALESCE(te.calls, 0) AS trace_calls,
               COALESCE(rr.repeated_reads, 0) AS repeated_reads
        FROM subagent_runs sr
        LEFT JOIN (
            SELECT stream_id, SUM(is_error) AS errors, COUNT(*) AS calls
            FROM tool_calls WHERE session_id = ?
            GROUP BY stream_id
        ) te ON te.stream_id = sr.agent_run_id
        LEFT JOIN (
            SELECT stream_id, SUM(c - 1) AS repeated_reads FROM (
                SELECT stream_id, COUNT(*) AS c FROM tool_calls
                WHERE session_id = ? AND category = 'read' AND target IS NOT NULL
                GROUP BY stream_id, target HAVING COUNT(*) > 1
            ) GROUP BY stream_id
        ) rr ON rr.stream_id = sr.agent_run_id
        WHERE sr.session_id = ?
        ORDER BY sr.total_tokens DESC
        """,
        (session_id, session_id, session_id),
    ).fetchall()


def session_tool_calls(
    conn: sqlite3.Connection,
    session_id: str,
    stream_id: str = "main",
    limit: int = 200,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Ordered tool calls of one stream ('main' or a subagent_run_id)."""
    return conn.execute(
        """
        SELECT idx, call_id, tool_name, category, target, ts, duration_ms,
               exit_code, is_error, input_preview, output_preview
        FROM tool_calls
        WHERE session_id = ? AND stream_id = ?
        ORDER BY idx
        LIMIT ? OFFSET ?
        """,
        (session_id, stream_id, limit, offset),
    ).fetchall()


def session_messages(
    conn: sqlite3.Connection,
    session_id: str,
    stream_id: str = "main",
    limit: int = 200,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Ordered messages of one stream ('main' or a subagent_run_id)."""
    return conn.execute(
        """
        SELECT idx, role, ts, model, content_preview, input_tokens, output_tokens
        FROM messages
        WHERE session_id = ? AND stream_id = ?
        ORDER BY idx
        LIMIT ? OFFSET ?
        """,
        (session_id, stream_id, limit, offset),
    ).fetchall()
