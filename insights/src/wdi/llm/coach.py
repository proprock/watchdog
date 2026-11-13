"""Dashboard-level coaching: aggregate metrics -> LLM advice, cached per filter."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from wdi.db.repository import Repository
from wdi.llm.analyzers import _parse_response, _template, _version
from wdi.llm.backend import LLMBackend
from wdi.metrics.compute import MetricsReport
from wdi.metrics.filters import SessionFilter

# Keep the metrics payload compact; aggregates are small but lists are capped.
_MAX_LIST_ITEMS = 5


def filter_hash(flt: SessionFilter) -> str:
    """Stable cache key for the filter a coach report was built for."""
    values: dict[str, object] = {
        "agent": flt.agent,
        "project": flt.project,
        "since": flt.since,
        "include_noise": flt.include_noise,
    }
    for name in ("branch", "repository_changed", "instructions_changed"):
        value = getattr(flt, name)
        if value is not None:
            values[name] = value
    canonical = json.dumps(values, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def coach_prompt_version() -> str:
    return _version(_template("coach.txt"))


def build_coach_context(report: MetricsReport) -> str:
    """Compact JSON payload of the aggregate metrics the coach reasons over."""
    ov = report.overview
    payload: dict[str, Any] = {
        "overview": {
            "sessions": ov.sessions,
            "claude_sessions": ov.claude_sessions,
            "codex_sessions": ov.codex_sessions,
            "input_tokens": ov.input_tokens,
            "output_tokens": ov.output_tokens,
            "cache_read_tokens": ov.cache_read_tokens,
            "cache_creation_tokens": ov.cache_creation_tokens,
            "cache_hit_rate": ov.cache_hit_rate,
            "tool_calls": ov.tool_calls,
            "compactions": ov.compactions,
            "api_errors": ov.api_errors,
            "estimated_cost_usd": ov.estimated_cost,
        },
    }
    if report.waste is not None:
        w = report.waste
        payload["waste"] = {
            "repeated_reads": w.repeated_reads,
            "duplicate_commands": w.duplicate_commands,
            "failed_calls": w.failed_calls,
            "failed_output_bytes": w.failed_output_bytes,
            "truncated_outputs_max_tokens": w.truncated_outputs,
        }
    if report.cache_misses is not None:
        m = report.cache_misses
        payload["cache_misses"] = {
            "cold_start": m.cold_start,
            "tool_wait": m.tool_wait,
            "user_idle": m.user_idle,
            "other": m.other,
            "wasted_tokens": m.wasted_tokens,
            "estimated_cost_usd": m.estimated_cost,
        }
    payload["tools"] = [
        {
            "tool": t.tool_name,
            "calls": t.calls,
            "errors": t.errors,
            "error_rate": round(t.error_rate, 3),
            "p95_latency_ms": t.p95_ms,
        }
        for t in report.tool_details[:_MAX_LIST_ITEMS]
    ]
    payload["shell_failure_clusters"] = [
        {"command_kind": c.command_kind, "calls": c.calls, "errors": c.errors}
        for c in report.shell_failure_clusters[:_MAX_LIST_ITEMS]
    ]
    payload["subagents"] = [
        {
            "type": s.agent_type,
            "runs": s.runs,
            "total_tokens": s.total_tokens,
            "search_to_edit": s.search_to_edit,
        }
        for s in report.subagents[:_MAX_LIST_ITEMS]
    ]
    payload["subagent_opportunities"] = len(report.subagent_opportunities)
    payload["top_repeated_files"] = [
        {"file": r.target, "wasted_reads": r.wasted_reads}
        for r in report.repeated_targets[:_MAX_LIST_ITEMS]
    ]
    if report.cache_trend:
        first, last = report.cache_trend[0], report.cache_trend[-1]
        payload["cache_trend"] = {
            "first_period": first.period,
            "first_hit_rate": first.hit_rate,
            "last_period": last.period,
            "last_hit_rate": last.hit_rate,
        }
    return json.dumps(payload, ensure_ascii=False)


def build_coach_prompt(report: MetricsReport) -> str:
    return _template("coach.txt").replace("{context}", build_coach_context(report))


@dataclass(slots=True)
class CoachResult:
    result: dict[str, Any]
    model: str
    created_at: str | None
    cached: bool
    stale: bool = False


def coach_cache_key(flt: SessionFilter, backend_name: str, model: str) -> str:
    value = json.dumps([filter_hash(flt), backend_name, model])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def context_hash(report: MetricsReport) -> str:
    return hashlib.sha256(build_coach_context(report).encode("utf-8")).hexdigest()


def get_cached_coach(
    conn: sqlite3.Connection,
    flt: SessionFilter,
    report: MetricsReport,
    *,
    model: str,
    backend_name: str,
) -> CoachResult | None:
    row = Repository(conn).get_coach_report(
        coach_cache_key(flt, backend_name, model), coach_prompt_version()
    )
    if row is None:
        return None
    try:
        stored = json.loads(row["result"])
    except json.JSONDecodeError:
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("result"), dict):
        return None
    return CoachResult(
        result=stored["result"],
        model=row["model"],
        created_at=row["created_at"],
        cached=True,
        stale=stored.get("context_hash") != context_hash(report),
    )


def run_coach(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    flt: SessionFilter,
    report: MetricsReport,
    *,
    model: str,
    force: bool = False,
    backend_name: str = "claude",
) -> CoachResult:
    """Return advice cached for this provider, model and current metrics."""
    if not force:
        cached = get_cached_coach(conn, flt, report, model=model, backend_name=backend_name)
        if cached is not None and not cached.stale:
            return cached
    text = backend.complete(build_coach_prompt(report), model)
    _score, data = _parse_response(text)
    Repository(conn).upsert_coach_report(
        coach_cache_key(flt, backend_name, model),
        prompt_version=coach_prompt_version(),
        model=model,
        result={"context_hash": context_hash(report), "result": data},
    )
    return CoachResult(result=data, model=model, created_at=None, cached=False)
