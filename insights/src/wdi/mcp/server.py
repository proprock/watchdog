"""FastMCP server wiring the metric queries as read-only MCP tools."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from mcp.server.fastmcp import FastMCP

from wdi.config import Settings
from wdi.db.connection import connect
from wdi.metrics import queries
from wdi.metrics.filters import SessionFilter


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the MCP server. Every tool opens a fresh read-only connection."""
    mcp = FastMCP("watchdog-insights")

    @contextmanager
    def _db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @mcp.tool()
    def get_recent_sessions(
        agent: str | None = None,
        project: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Recent agent sessions (newest first) with tokens and tool-call counts.

        Optional filters: agent ('claude'/'codex'), project canonical name, and
        since (ISO date '2026-06-01' or relative span like '7d').
        """
        flt = SessionFilter(agent=agent, project=project, since=since)
        with _db() as conn:
            return [dict(row) for row in queries.session_list(conn, flt, limit)]

    @mcp.tool()
    def get_tool_failure_patterns(
        agent: str | None = None,
        project: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Tools that produced errors, ranked by error count, with error_rate."""
        flt = SessionFilter(agent=agent, project=project, since=since)
        with _db() as conn:
            rows = queries.top_failing_tools(conn, flt, limit)
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            calls = item.get("calls") or 0
            errors = item.get("errors") or 0
            item["error_rate"] = (errors / calls) if calls else 0.0
            result.append(item)
        return result

    @mcp.tool()
    def get_oversearching_examples(
        agent: str | None = None,
        project: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Sessions with search-heavy / edit-light tool use and redundant re-reads."""
        flt = SessionFilter(agent=agent, project=project, since=since)
        with _db() as conn:
            return [dict(row) for row in queries.oversearching_sessions(conn, flt, limit)]

    @mcp.tool()
    def get_prompt_quality_report(
        agent: str | None = None,
        project: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Stored prompt-quality analyses (worst score first).

        Requires sessions analyzed beforehand with `wdi analyze prompt-quality`;
        un-analyzed sessions are omitted.
        """
        flt = SessionFilter(agent=agent, project=project, since=since)
        with _db() as conn:
            rows = queries.prompt_quality_reports(conn, flt, limit)
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            raw = item.get("result")
            if isinstance(raw, str):
                with suppress(json.JSONDecodeError):
                    item["result"] = json.loads(raw)
            result.append(item)
        return result

    return mcp
