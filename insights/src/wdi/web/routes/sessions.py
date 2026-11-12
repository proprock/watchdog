"""Session routes: filterable list and per-session detail."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from wdi.llm import transcript
from wdi.metrics import queries
from wdi.metrics.filters import SessionFilter
from wdi.report.charts import embed_json, session_chart_data
from wdi.web import badges
from wdi.web.dependencies import DbDep, PricingDep, TemplatesDep
from wdi.web.filters import FilterDep

router = APIRouter()


def _avg_duration(conn: sqlite3.Connection, flt: SessionFilter) -> float | None:
    row = queries.overview(conn, flt)
    sessions = row["sessions"] or 0
    return (row["total_duration_ms"] or 0) / sessions if sessions else None


# Human labels for the analysis_type values stored in llm_analyses.
_ANALYSIS_LABELS = {
    "prompt_quality": "Prompt quality",
    "session_summary": "Session summary",
    "agent_efficiency": "Agent efficiency",
}


def _shape_analyses(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Parse stored JSON results into a render-friendly shape for the template."""
    shaped: list[dict[str, object]] = []
    for row in rows:
        try:
            data = json.loads(row["result"])
        except (json.JSONDecodeError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        advice = data.get("advice")
        shaped.append(
            {
                "type": row["analysis_type"],
                "label": _ANALYSIS_LABELS.get(row["analysis_type"], row["analysis_type"]),
                "score": row["score"],
                "model": row["model"],
                "created_at": row["created_at"],
                "summary": data.get("summary"),
                "advice": advice if isinstance(advice, list) else [],
                "raw": data.get("raw"),
            }
        )
    return shaped


@router.get("/sessions", response_class=HTMLResponse)
def session_list(
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    flt: FilterDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> HTMLResponse:
    rows = [dict(r) for r in queries.session_list(conn, flt, limit)]
    avg_duration = _avg_duration(conn, flt)
    for row in rows:
        row["badges"] = badges.session_badges(row, avg_duration)
    context = {
        "rows": rows,
        "filter": flt,
        "limit": limit,
        "legend": badges.LEGEND,
        "filter_action": "/sessions",
        "filter_target": "#session-table",
        "project_names": queries.project_names(conn),
    }
    name = (
        "partials/_session_table.html"
        if request.headers.get("HX-Request")
        else "pages/sessions.html"
    )
    return templates.TemplateResponse(request, name, context)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(
    session_id: str,
    request: Request,
    conn: DbDep,
    pricing: PricingDep,
    templates: TemplatesDep,
) -> HTMLResponse:
    row = queries.session_detail(conn, session_id)
    if row is None:
        return templates.TemplateResponse(
            request, "pages/not_found.html", {"session_id": session_id}, status_code=404
        )
    detail = dict(row)
    sid = detail["session_id"]
    detail["estimated_cost"] = pricing.cost(
        detail["model"],
        input_tokens=detail["input_tokens"],
        output_tokens=detail["output_tokens"],
        cache_read_tokens=detail["cache_read_tokens"],
        cache_creation_tokens=detail["cache_creation_tokens"] or 0,
    )
    detail["badges"] = badges.session_badges(detail, _avg_duration(conn, flt=SessionFilter()))
    subagents = [dict(a) for a in queries.session_subagents(conn, sid)]
    for run in subagents:
        run["badges"] = badges.subagent_badges(run)
    curve = queries.session_context_curve(conn, sid)
    context = {
        "s": detail,
        "categories": [dict(c) for c in queries.session_tool_categories(conn, sid)],
        "subagents": subagents,
        "analyses": _shape_analyses(queries.session_analyses(conn, sid)),
        "legend": badges.LEGEND,
        "waste": queries.session_waste(conn, sid),
        "repeated_targets": queries.session_repeated_targets(conn, sid),
        # Chart section renders only when the stream carries usage data
        # (Claude assistant rows or Codex token_count rows).
        "has_context_curve": any(r["subtype"] in (None, "token_count") for r in curve),
        "session_chart_data": embed_json(session_chart_data(curve)),
    }
    return templates.TemplateResponse(request, "pages/session_detail.html", context)


@router.get("/sessions/{session_id}/stream/{stream_id}", response_class=HTMLResponse)
def stream_detail(
    session_id: str,
    stream_id: str,
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """Tool calls + messages of one stream ('main' = user session, else a subagent)."""
    session = queries.session_detail(conn, session_id)
    if session is None:
        return templates.TemplateResponse(
            request, "pages/not_found.html", {"session_id": session_id}, status_code=404
        )
    tool_calls = [
        dict(c) for c in queries.session_tool_calls(conn, session_id, stream_id, limit, offset)
    ]
    messages = [
        dict(m) for m in queries.session_messages(conn, session_id, stream_id, limit, offset)
    ]
    context = {
        "s": dict(session),
        "stream_id": stream_id,
        "is_main": stream_id == "main",
        "tool_calls": tool_calls,
        "messages": messages,
    }
    template = (
        "partials/_stream.html" if request.headers.get("HX-Request") else "pages/stream_detail.html"
    )
    return templates.TemplateResponse(request, template, context)


@router.get(
    "/sessions/{session_id}/stream/{stream_id}/call/{call_id}/full",
    response_class=HTMLResponse,
)
def tool_call_full(
    session_id: str,
    stream_id: str,
    call_id: str,
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
) -> HTMLResponse:
    """Full tool input/output read from the source transcript, with preview fallback."""
    session = queries.session_detail(conn, session_id)
    source = session["source_path"] if session is not None else None
    full_input: str | None = None
    full_output: str | None = None
    if isinstance(source, str):
        full_input, full_output = transcript.find_tool_io(Path(source), stream_id, call_id)
    available = full_input is not None or full_output is not None
    if not available:
        # Fall back to the stored previews (e.g. snapshot import without local source).
        row = conn.execute(
            """
            SELECT input_preview, output_preview FROM tool_calls
            WHERE session_id = ? AND stream_id = ? AND call_id = ?
            LIMIT 1
            """,
            (session_id, stream_id, call_id),
        ).fetchone()
        if row is not None:
            full_input, full_output = row["input_preview"], row["output_preview"]
    context = {
        "full_input": full_input,
        "full_output": full_output,
        "from_source": available,
    }
    return templates.TemplateResponse(request, "partials/_tool_call_full.html", context)
