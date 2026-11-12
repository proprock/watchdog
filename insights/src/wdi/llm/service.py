"""Orchestrate LLM analysis: select sessions, call backend, cache results."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from wdi.db.repository import Repository
from wdi.llm.analyzers import ANALYZERS, Analyzer
from wdi.llm.backend import LLMBackend, LLMError
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver

# Rough per-call output budget: analyzers return a small JSON answer.
_EST_OUTPUT_TOKENS_PER_CALL = 600

# `wdi analyze` runs with short model aliases the claude CLI understands, but the
# pricing table is keyed by full model ids; map aliases for the cost estimate.
_MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


@dataclass(slots=True)
class AnalysisStats:
    analyzed: int = 0
    skipped_not_applicable: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisEstimate:
    """Pre-flight estimate of the work one analyzer run would send to the LLM."""

    analyzer: str
    model: str
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


def _estimate_tokens(text: str) -> int:
    # Rough heuristic: ~4 characters per token. Good enough for a pre-flight figure.
    return max(1, (len(text) + 3) // 4)


def estimate_analyzer(
    conn: sqlite3.Connection,
    analyzer: Analyzer,
    flt: SessionFilter,
    *,
    model: str,
    limit: int,
    pricing: PricingResolver,
    force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> AnalysisEstimate:
    """Estimate sessions, tokens and cost without calling the LLM.

    Builds the same prompts ``run_analyzer`` would send (DB reads only) so the
    user can confirm before a potentially expensive batch.
    """
    repo = Repository(conn)
    where, params = flt.where()
    candidates = repo.sessions_for_analysis(
        analyzer.analysis_type, analyzer.version, where, params, limit, force=force
    )
    estimate = AnalysisEstimate(analyzer=analyzer.key, model=model)
    total = len(candidates)
    for done, row in enumerate(candidates, start=1):
        session_id = row["session_id"]
        if not analyzer.applies(conn, session_id):
            if on_progress is not None:
                on_progress(done, total)
            continue
        estimate.sessions += 1
        estimate.input_tokens += _estimate_tokens(analyzer.build_prompt(conn, session_id))
        if on_progress is not None:
            on_progress(done, total)
    estimate.output_tokens = estimate.sessions * _EST_OUTPUT_TOKENS_PER_CALL
    estimate.cost = pricing.cost(
        _MODEL_ALIASES.get(model, model),
        input_tokens=estimate.input_tokens,
        output_tokens=estimate.output_tokens,
    )
    return estimate


def run_analyzer(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    analyzer: Analyzer,
    flt: SessionFilter,
    *,
    model: str,
    limit: int,
    force: bool = False,
    workers: int = 1,
    on_progress: Callable[[bool], None] | None = None,
) -> AnalysisStats:
    """Run one analyzer over matching sessions.

    When *workers* > 1 the LLM calls are issued concurrently via a thread pool
    while all DB reads and writes remain on the calling thread.
    *on_progress* is called with ``True`` (success) or ``False`` (failure) after
    each session's LLM call completes.
    """
    repo = Repository(conn)
    stats = AnalysisStats()
    where, params = flt.where()
    candidates = repo.sessions_for_analysis(
        analyzer.analysis_type, analyzer.version, where, params, limit, force=force
    )

    # Phase 1: collect applicable sessions and build prompts (DB reads, main thread).
    work_items: list[tuple[str, str, str]] = []
    for row in candidates:
        session_id = row["session_id"]
        if not analyzer.applies(conn, session_id):
            stats.skipped_not_applicable += 1
            continue
        prompt = analyzer.build_prompt(conn, session_id)
        work_items.append((session_id, row["machine_id"], prompt))

    def _call_llm(
        item: tuple[str, str, str],
    ) -> tuple[str, str, str | None, LLMError | None]:
        sid, mid, prompt = item
        try:
            return sid, mid, backend.complete(prompt, model), None
        except LLMError as exc:
            return sid, mid, None, exc

    def _store(sid: str, mid: str, text: str | None, err: LLMError | None) -> None:
        if err is not None:
            stats.failed += 1
            stats.errors.append(f"{sid[:8]}: {err}")
        else:
            # Preserve the existing analyzer result handling.
            score, result = analyzer.parse(text)  # ty: ignore[invalid-argument-type]
            repo.upsert_llm_analysis(
                sid,
                mid,
                analysis_type=analyzer.analysis_type,
                prompt_version=analyzer.version,
                model=model,
                score=score,
                result=result,
            )
            stats.analyzed += 1
        if on_progress is not None:
            on_progress(err is None)

    # Phase 2+3: LLM calls (parallel when workers > 1) and sequential DB writes.
    if workers > 1 and len(work_items) > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_call_llm, item) for item in work_items]
            for future in as_completed(futures):
                _store(*future.result())
    else:
        for item in work_items:
            _store(*_call_llm(item))

    return stats


def resolve_analyzers(type_key: str | None) -> list[Analyzer]:
    if type_key is None:
        return list(ANALYZERS.values())
    if type_key not in ANALYZERS:
        raise KeyError(type_key)
    return [ANALYZERS[type_key]]
