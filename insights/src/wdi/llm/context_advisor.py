"""Optional LLM advice over privacy-preserving context aggregates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from wdi.context_analysis import ContextAnalysisFilter, ContextAnalysisReport
from wdi.db.repository import Repository
from wdi.llm.analyzers import _parse_response, _template, _version
from wdi.llm.backend import LLMBackend


@dataclass(slots=True)
class ContextAdvisorResult:
    result: dict[str, Any]
    model: str
    created_at: str | None
    cached: bool
    stale: bool = False


def prompt_version() -> str:
    return _version(_template("context_strategy.txt"))


def build_context(report: ContextAnalysisReport) -> str:
    """Serialize aggregates only; never include titles, paths, IDs, or raw event data."""
    payload = {
        "totals": {
            "sessions": report.total_sessions,
            "model_calls": report.total_model_calls,
            "compactions": report.total_compactions,
        },
        "local_advisor": {
            "strategy": report.advice.strategy,
            "revise_compact_prompt": report.advice.revise_compact_prompt,
        },
        "providers": [
            {
                "provider": item.agent,
                "sessions": item.sessions,
                "model_calls": item.model_calls,
                "compactions": item.compactions,
                "median_peak_context_tokens": item.median_peak_context_tokens,
                "median_peak_visible_bytes": item.median_peak_visible_bytes,
                "repeated_bytes": item.repeated_bytes,
                "category_bytes": item.category_bytes,
                "category_changes": item.category_changes,
                "median_compaction_reduction": item.median_compaction_reduction,
                "repeated_after_compaction_rate": item.repeated_after_compaction_rate,
                "recovery_after_calls": {
                    "1": item.recovery_1,
                    "3": item.recovery_3,
                    "5": item.recovery_5,
                },
            }
            for item in report.providers
        ],
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def build_prompt(report: ContextAnalysisReport) -> str:
    return _template("context_strategy.txt").replace("{context}", build_context(report))


def _filter_hash(flt: ContextAnalysisFilter, backend_name: str, model: str) -> str:
    canonical = json.dumps(
        {
            "namespace": "context-strategy-v2",
            "scope": flt.scope,
            "selected_ids": sorted(flt.selected_ids),
            "top_n": flt.top_n if flt.scope == "top" else None,
            "sort": flt.sort if flt.scope == "top" else None,
            "agent": flt.agent,
            "project": flt.project,
            "since": flt.since,
            "until": flt.until,
            "include_noise": flt.include_noise,
            "min_calls": flt.min_calls,
            "min_turns": flt.min_turns,
            "provider": backend_name,
            "model": model,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _context_hash(report: ContextAnalysisReport) -> str:
    return hashlib.sha256(build_context(report).encode("utf-8")).hexdigest()


def get_cached(
    conn: sqlite3.Connection,
    flt: ContextAnalysisFilter,
    report: ContextAnalysisReport,
    *,
    backend_name: str,
    model: str,
) -> ContextAdvisorResult | None:
    row = Repository(conn).get_coach_report(
        _filter_hash(flt, backend_name, model), prompt_version()
    )
    if row is None:
        return None
    try:
        stored = json.loads(row["result"])
    except json.JSONDecodeError:
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("result"), dict):
        return None
    return ContextAdvisorResult(
        result=stored["result"],
        model=row["model"],
        created_at=row["created_at"],
        cached=True,
        stale=stored.get("context_hash") != _context_hash(report),
    )


def run(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    flt: ContextAnalysisFilter,
    report: ContextAnalysisReport,
    *,
    backend_name: str,
    model: str,
    force: bool = False,
) -> ContextAdvisorResult:
    if not force:
        cached = get_cached(conn, flt, report, backend_name=backend_name, model=model)
        if cached is not None and not cached.stale:
            return cached
    text = backend.complete(build_prompt(report), model)
    _score, result = _parse_response(text)
    Repository(conn).upsert_coach_report(
        _filter_hash(flt, backend_name, model),
        prompt_version=prompt_version(),
        model=model,
        result={"context_hash": _context_hash(report), "result": result},
    )
    return ContextAdvisorResult(result=result, model=model, created_at=None, cached=False)
