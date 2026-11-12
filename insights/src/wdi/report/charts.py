"""Shared presentation helpers for the static report and the web server.

Both the offline HTML report and the live FastAPI server render the same
metrics, so the Chart.js data shaping and the Jinja filter set live here to
avoid duplication.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from wdi.cli_format import (
    display_name,
    fmt_cost,
    fmt_duration,
    fmt_int,
    fmt_pct,
    fmt_ratio,
    fmt_tokens,
)

if TYPE_CHECKING:
    import sqlite3

    from jinja2 import Environment

    from wdi.metrics.compute import MetricsReport


def register_jinja_filters(env: Environment) -> None:
    """Register the formatting filters used by every metrics template."""
    env.filters["tokens"] = fmt_tokens
    env.filters["int_"] = fmt_int
    env.filters["cost"] = fmt_cost
    env.filters["pct"] = fmt_pct
    env.filters["duration"] = fmt_duration
    env.filters["ratio"] = fmt_ratio
    env.filters["name"] = display_name


def chart_data(report: MetricsReport) -> dict[str, object]:
    """Shape a report into the JSON structure the Chart.js client expects."""
    ov = report.overview
    breakdown = [
        (label, value)
        for label, value in (
            ("Input", ov.input_tokens),
            ("Output", ov.output_tokens),
            ("Cache read", ov.cache_read_tokens),
            ("Reasoning", ov.reasoning_tokens),
        )
        if value
    ]
    return {
        "availability": asdict(report.availability),
        "activity": {
            "labels": [b.period for b in report.activity],
            "sessions": [b.sessions for b in report.activity],
            "claude": [b.claude_sessions for b in report.activity],
            "codex": [b.codex_sessions for b in report.activity],
            "tokens": [b.tokens for b in report.activity],
        },
        "tools": {
            "labels": [c.category for c in report.tool_categories],
            "calls": [c.calls for c in report.tool_categories],
            "errors": [c.errors for c in report.tool_categories],
        },
        "models": {
            "labels": [m.model for m in report.models],
            "tokens": [m.input_tokens + m.output_tokens for m in report.models],
        },
        "token_breakdown": {
            "labels": [label for label, _ in breakdown],
            "values": [value for _, value in breakdown],
        },
        "cache_trend": {
            "labels": [b.period for b in report.cache_trend],
            "input": [b.input_tokens for b in report.cache_trend],
            "cache_read": [b.cache_read_tokens for b in report.cache_trend],
            "cache_creation": [b.cache_creation_tokens for b in report.cache_trend],
        },
        "cache_misses": {
            "labels": ["Cold start", "Tool wait", "User idle", "Other"],
            "values": [
                report.cache_misses.cold_start,
                report.cache_misses.tool_wait,
                report.cache_misses.user_idle,
                report.cache_misses.other,
            ]
            if report.cache_misses
            else [0, 0, 0, 0],
        },
    }


def session_chart_data(curve: list[sqlite3.Row]) -> dict[str, object]:
    """Shape a session context curve for the per-session Chart.js line chart.

    Compaction rows become vertical markers; Claude assistant rows and Codex
    token_count rows form the line.
    """
    points = [
        {"idx": row["idx"], "context": row["context_tokens"], "output": row["output_tokens"] or 0}
        for row in curve
        if row["subtype"] in (None, "token_count")
    ]
    compactions = [
        {"idx": row["idx"], "pre": row["pre_tokens"], "post": row["post_tokens"]}
        for row in curve
        if row["subtype"] == "compact_boundary"
    ]
    return {"context_curve": {"points": points, "compactions": compactions}}


def embed_json(data: dict[str, object]) -> str:
    """Serialize chart data for a <script> island, escaping "<" to stay inside it."""
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
