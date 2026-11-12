"""Reconstruct observable context snapshots and derive compaction guidance."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from wdi.metrics.pricing import PricingResolver

_SORT_COLUMNS = {
    "model_calls": "model_calls",
    "turns": "turns",
    "input_tokens": "input_tokens",
    "peak_context_tokens": "peak_context_tokens",
    "visible_context_bytes": "peak_visible_bytes",
    "compactions": "compactions",
    "repeated_bytes": "repeated_bytes",
    "input_uncached": "input_uncached",
    "cache_read": "cache_read",
    "cache_write": "cache_write",
    "output": "output",
    "cost": "cost_sort",
}

COMPACT_PROMPT_DRAFT = """Preserve the working state in a compact continuation:
Goal: the exact requested outcome and success criteria.
Current state: what is implemented, observed, or still in progress.
Decisions: chosen approaches, constraints, and rejected alternatives that still matter.
Relevant files and symbols: only paths and names needed to continue.
Commands and results: important diagnostics, failures, and verified outcomes.
Verification: tests/checks already run and their status.
Blockers: unresolved risks, missing input, or external state.
Next step: the single most useful concrete action.
Keep instructions functionally equivalent and omit superseded detail."""


@dataclass(slots=True)
class ContextAnalysisFilter:
    agent: str | None = None
    project: str | None = None
    since: str | None = None
    until: str | None = None
    include_noise: bool = False
    min_calls: int = 1
    min_turns: int = 0
    top_n: int = 20
    sort: str = "input_tokens"
    session_id: str | None = None
    selected_ids: tuple[str, ...] = ()
    scope: str = "filtered"


@dataclass(slots=True)
class ContextSnapshot:
    call_index: int
    ts: str | None
    turn_id: str | None
    generation: int
    categories: dict[str, int]
    item_counts: dict[str, int]
    visible_bytes: int
    added_bytes: int
    repeated_bytes: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    context_window: int | None
    context_tokens: int
    utilization: float | None
    coverage: str
    input_uncached: int = 0
    changed_target_bytes: int = 0
    overlap_bytes: int = 0
    error_bytes: int = 0
    estimated_visible_tokens: int = 0
    model: str | None = None
    category_changes: dict[str, dict[str, int]] = field(default_factory=dict)
    compact_trigger: str | None = None
    compact_pre_tokens: int | None = None
    compact_post_tokens: int | None = None


@dataclass(slots=True)
class SessionContextSummary:
    session_id: str
    agent: str
    project: str | None
    title: str | None
    started_at: str | None
    turns: int
    model_calls: int
    input_tokens: int
    peak_context_tokens: int
    peak_visible_bytes: int
    compactions: int
    repeated_bytes: int
    snapshots: list[ContextSnapshot] = field(repr=False)
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cost: float | None = None
    cost_sort: float = -1
    growth: list[Mapping[str, Any]] = field(default_factory=list)
    stream_calls: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderContextSummary:
    agent: str
    sessions: int
    model_calls: int
    compactions: int
    median_peak_context_tokens: float
    median_peak_visible_bytes: float
    repeated_bytes: int
    category_bytes: dict[str, int]
    median_compaction_reduction: float | None
    repeated_after_compaction_rate: float | None
    recovery_1: float | None
    recovery_3: float | None
    recovery_5: float | None
    category_changes: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(slots=True)
class ContextAdvice:
    strategy: str
    summary: str
    recommendations: list[str]
    revise_compact_prompt: bool
    compact_prompt_draft: str = COMPACT_PROMPT_DRAFT


@dataclass(slots=True)
class ContextAnalysisReport:
    sessions: list[SessionContextSummary]
    providers: list[ProviderContextSummary]
    selected: SessionContextSummary | None
    advice: ContextAdvice
    total_sessions: int
    total_model_calls: int
    total_compactions: int
    matched_sessions: int = 0
    analyzed_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _Block:
    key: str
    category: str
    byte_count: int
    fingerprint: str | None
    estimated_tokens: int = 0


def _extra(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("extra")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def reconstruct_snapshots(rows: Sequence[Mapping[str, Any]]) -> list[ContextSnapshot]:
    """Rebuild the observable active context at every usage-bearing model call."""
    active: dict[str, _Block] = {}
    instruction_slots: dict[str, str] = {}
    pending_outputs: list[Mapping[str, Any]] = []
    seen_fingerprints: set[str] = set()
    seen_target_fingerprints: set[str] = set()
    seen_lines: set[str] = set()
    changes: dict[str, Counter[str]] = defaultdict(Counter)
    changed_target_bytes = 0
    overlap_bytes = 0
    error_bytes = 0
    snapshots: list[ContextSnapshot] = []
    added_bytes = 0
    repeated_bytes = 0
    opaque = False
    last_call_index = -1
    compact_trigger: str | None = None
    compact_pre_tokens: int | None = None
    compact_post_tokens: int | None = None

    def add_block(row: Mapping[str, Any]) -> None:
        nonlocal added_bytes, repeated_bytes, changed_target_bytes, overlap_bytes, error_bytes
        byte_count = row.get("byte_count")
        if not isinstance(byte_count, int):
            return
        category = str(row.get("category") or "other")
        fingerprint_value = row.get("fingerprint")
        fingerprint_text = fingerprint_value if isinstance(fingerprint_value, str) else None
        target_value = row.get("target_fingerprint")
        target_text = target_value if isinstance(target_value, str) else None
        extra = _extra(row)
        slot = extra.get("slot")
        if isinstance(slot, str):
            previous = instruction_slots.get(slot)
            if previous:
                prior = active.get(previous)
                if (
                    prior
                    and prior.fingerprint == fingerprint_text
                    and prior.byte_count == byte_count
                ):
                    return
                active.pop(previous, None)
        key = f"{row.get('idx')}:{len(active)}"
        if isinstance(slot, str):
            instruction_slots[slot] = key
        active[key] = _Block(
            key, category, byte_count, fingerprint_text, int(extra.get("estimated_tokens") or 0)
        )
        added_bytes += byte_count
        changes[category]["added_bytes"] += byte_count
        changes[category]["estimated_tokens"] += int(extra.get("estimated_tokens") or 0)
        repeated_content = (
            fingerprint_text is not None
            and fingerprint_text in seen_fingerprints
            and not extra.get("replacement")
        )
        target_key = f"{row.get('event_type')}:{category}:{target_text}"
        repeated_target = target_text is not None and target_key in seen_target_fingerprints
        if repeated_content:
            repeated_bytes += byte_count
            changes[category]["exact_repeat_bytes"] += byte_count
        if repeated_target and not repeated_content:
            changed_target_bytes += byte_count
            changes[category]["changed_target_bytes"] += byte_count
        lines = set(extra.get("line_hashes") or [])
        if lines and not repeated_content:
            overlap = int(byte_count * len(lines & seen_lines) / len(lines))
            overlap_bytes += overlap
            changes[category]["overlap_bytes"] += overlap
        seen_lines.update(lines)
        if extra.get("is_error") is True:
            error_bytes += byte_count
            changes[category]["error_bytes"] += byte_count
        if fingerprint_text:
            seen_fingerprints.add(fingerprint_text)
        if target_text:
            seen_target_fingerprints.add(target_key)

    def activate_event(row: Mapping[str, Any]) -> None:
        nonlocal opaque
        if row.get("coverage") == "opaque":
            opaque = True
        else:
            add_block(row)

    for row in rows:
        event_type = row.get("event_type")
        extra = _extra(row)
        if event_type == "compact_boundary":
            active.clear()
            instruction_slots.clear()
            pending_outputs.clear()
            added_bytes = 0
            repeated_bytes = 0
            opaque = True
            changed_target_bytes = overlap_bytes = error_bytes = 0
            changes.clear()
            trigger = extra.get("trigger")
            compact_trigger = trigger if isinstance(trigger, str) else "unknown"
            pre_tokens = extra.get("pre_tokens")
            post_tokens = extra.get("post_tokens")
            compact_pre_tokens = pre_tokens if isinstance(pre_tokens, int) else None
            compact_post_tokens = post_tokens if isinstance(post_tokens, int) else None
            continue
        if event_type in ("compact_marker", "usage_observation"):
            continue
        if event_type == "model_call":
            categories = Counter(block.category for block in active.values())
            category_bytes: Counter[str] = Counter()
            for block in active.values():
                category_bytes[block.category] += block.byte_count
            context_tokens = sum(
                int(row.get(key) or 0)
                for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            )
            if extra.get("provider") == "codex":
                context_tokens = int(row.get("input_tokens") or 0)
            input_uncached = max(
                0,
                context_tokens
                - int(row.get("cache_read_tokens") or 0)
                - int(row.get("cache_creation_tokens") or 0),
            )
            context_window = row.get("context_window")
            utilization = (
                context_tokens / context_window
                if isinstance(context_window, int) and context_window > 0
                else None
            )
            snapshots.append(
                ContextSnapshot(
                    call_index=int(row.get("model_call_index") or 0),
                    ts=row.get("ts") if isinstance(row.get("ts"), str) else None,
                    turn_id=(row.get("turn_id") if isinstance(row.get("turn_id"), str) else None),
                    generation=int(row.get("compaction_generation") or 0),
                    categories=dict(category_bytes),
                    item_counts=dict(categories),
                    visible_bytes=sum(category_bytes.values()),
                    added_bytes=added_bytes,
                    repeated_bytes=repeated_bytes,
                    input_tokens=int(row.get("input_tokens") or 0),
                    output_tokens=int(row.get("output_tokens") or 0),
                    cache_read_tokens=int(row.get("cache_read_tokens") or 0),
                    cache_creation_tokens=int(row.get("cache_creation_tokens") or 0),
                    reasoning_tokens=int(row.get("reasoning_tokens") or 0),
                    context_window=context_window if isinstance(context_window, int) else None,
                    context_tokens=context_tokens,
                    input_uncached=input_uncached,
                    changed_target_bytes=changed_target_bytes,
                    overlap_bytes=overlap_bytes,
                    error_bytes=error_bytes,
                    estimated_visible_tokens=sum(
                        block.estimated_tokens for block in active.values()
                    ),
                    model=extra.get("model"),
                    category_changes={k: dict(v) for k, v in changes.items()},
                    utilization=utilization,
                    coverage=("partial" if active else "opaque") if opaque else "visible",
                    compact_trigger=compact_trigger,
                    compact_pre_tokens=compact_pre_tokens,
                    compact_post_tokens=compact_post_tokens,
                )
            )
            compact_trigger = None
            compact_pre_tokens = None
            compact_post_tokens = None
            added_bytes = 0
            repeated_bytes = 0
            changed_target_bytes = overlap_bytes = error_bytes = 0
            changes.clear()
            last_call_index = int(row.get("model_call_index") or 0)
            for pending in pending_outputs:
                activate_event(pending)
            pending_outputs.clear()
            continue

        direction = extra.get("direction", "input")
        call_index = row.get("model_call_index")
        if direction == "output" and isinstance(call_index, int) and call_index > last_call_index:
            pending_outputs.append(row)
        else:
            activate_event(row)
    return snapshots


def _normalize_date(value: str) -> str:
    if value.endswith("d") and value[:-1].isdigit():
        return (datetime.now(UTC) - timedelta(days=int(value[:-1]))).date().isoformat()
    return value


def _where(flt: ContextAnalysisFilter) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if flt.agent:
        clauses.append("s.agent = ?")
        params.append(flt.agent)
    if flt.project:
        clauses.append("p.canonical_name = ?")
        params.append(flt.project)
    if flt.since:
        clauses.append("s.started_at >= ?")
        params.append(_normalize_date(flt.since))
    if flt.until:
        clauses.append("s.started_at < date(?, '+1 day')")
        params.append(flt.until)
    if not flt.include_noise:
        clauses.append("s.is_noise = 0")
    return " AND ".join(clauses) if clauses else "1 = 1", params


def _compact_outcomes(
    summaries: list[SessionContextSummary],
) -> tuple[list[float], list[float], dict[int, list[float]]]:
    reductions: list[float] = []
    repeated: list[float] = []
    recoveries: dict[int, list[float]] = {1: [], 3: [], 5: []}
    for summary in summaries:
        snaps = summary.snapshots
        for index, snap in enumerate(snaps):
            if index == 0 or snap.generation <= snaps[index - 1].generation:
                continue
            before = snap.compact_pre_tokens or snaps[index - 1].context_tokens
            after = snap.compact_post_tokens or snap.context_tokens
            if before > 0:
                reductions.append((before - after) / before)
                for distance in recoveries:
                    target = index + distance - 1
                    if target < len(snaps):
                        recoveries[distance].append(snaps[target].context_tokens / before)
            following = snaps[index : index + 3]
            repeated.append(float(any(item.repeated_bytes > 0 for item in following)))
    return reductions, repeated, recoveries


def _provider_summary(agent: str, summaries: list[SessionContextSummary]) -> ProviderContextSummary:
    reductions, repeated, recoveries = _compact_outcomes(summaries)
    category_bytes: Counter[str] = Counter()
    changes: dict[str, Counter[str]] = defaultdict(Counter)
    for summary in summaries:
        for snapshot in summary.snapshots:
            category_bytes.update(snapshot.categories)
            for category, values in snapshot.category_changes.items():
                changes[category].update(values)
    return ProviderContextSummary(
        agent=agent,
        sessions=len(summaries),
        model_calls=sum(item.model_calls for item in summaries),
        compactions=sum(item.compactions for item in summaries),
        median_peak_context_tokens=median(item.peak_context_tokens for item in summaries),
        median_peak_visible_bytes=median(item.peak_visible_bytes for item in summaries),
        repeated_bytes=sum(item.repeated_bytes for item in summaries),
        category_bytes=dict(category_bytes),
        median_compaction_reduction=median(reductions) if reductions else None,
        repeated_after_compaction_rate=(sum(repeated) / len(repeated) if repeated else None),
        recovery_1=median(recoveries[1]) if recoveries[1] else None,
        recovery_3=median(recoveries[3]) if recoveries[3] else None,
        recovery_5=median(recoveries[5]) if recoveries[5] else None,
        category_changes={k: dict(v) for k, v in changes.items()},
    )


def _advisor(summaries: list[SessionContextSummary]) -> ContextAdvice:
    compactions = sum(item.compactions for item in summaries)
    reductions, repeated, recoveries = _compact_outcomes(summaries)
    if compactions < 5 or len(reductions) < 5:
        return ContextAdvice(
            strategy="insufficient evidence",
            summary="At least five measurable compactions are required for a strategy comparison.",
            recommendations=[
                "Keep collecting per-call snapshots before changing automatic compaction settings.",
                "Use milestone compaction only when a completed tool-heavy phase no longer "
                "needs raw output.",
            ],
            revise_compact_prompt=False,
        )

    median_reduction = median(reductions)
    repeated_rate = sum(repeated) / len(repeated) if repeated else 0.0
    recovery_5 = median(recoveries[5]) if recoveries[5] else None
    strategy = "experiment required"
    recommendations = [
        f"Median measured context reduction is {median_reduction:.0%}.",
        f"Repeated material appears within three calls after {repeated_rate:.0%} of compactions.",
    ]
    if recovery_5 is not None:
        recommendations.append(
            f"After five calls, median context returns to {recovery_5:.0%} of its "
            "pre-compact level."
        )
    revise = False
    if revise:
        recommendations.append(
            "Revise compact_prompt to preserve decisions, verification evidence, blockers, "
            "and the next step."
        )
    return ContextAdvice(
        strategy=strategy,
        summary=(
            "Observational data cannot establish which strategy saves tokens "
            "without loss of working state. Compare controlled continuations "
            "and include compaction overhead."
        ),
        recommendations=recommendations,
        revise_compact_prompt=revise,
    )


def build_context_report(
    conn: sqlite3.Connection, flt: ContextAnalysisFilter, pricing: PricingResolver | None = None
) -> ContextAnalysisReport:
    """Build rankings, per-provider aggregates and one selected per-call timeline."""
    where, params = _where(flt)
    session_rows = conn.execute(
        f"""
        SELECT s.session_id, s.agent, s.title, s.started_at, s.turn_count,
               s.peak_context_tokens, p.canonical_name AS project
        FROM sessions s LEFT JOIN projects p ON p.project_id = s.project_id
        WHERE {where}
        ORDER BY s.session_id
        """,  # noqa: S608 - clauses are fixed and values are parameterized
        params,
    ).fetchall()
    events: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    if session_rows:
        for row in conn.execute(
            f"""
            SELECT ce.* FROM context_events ce
            JOIN sessions s ON s.session_id = ce.session_id
            LEFT JOIN projects p ON p.project_id = s.project_id
            WHERE ce.stream_id = 'main' AND {where}
            ORDER BY ce.session_id, ce.idx
            """,  # noqa: S608 - clauses are fixed and values are parameterized
            params,
        ).fetchall():
            events[row["session_id"]].append(dict(row))

    eligible: list[SessionContextSummary] = []
    for row in session_rows:
        snapshots = reconstruct_snapshots(events[row["session_id"]])
        turns = int(row["turn_count"] or 0)
        if len(snapshots) < flt.min_calls or turns < flt.min_turns:
            continue
        eligible.append(
            SessionContextSummary(
                session_id=row["session_id"],
                agent=row["agent"],
                project=row["project"],
                title=row["title"],
                started_at=row["started_at"],
                turns=turns,
                model_calls=len(snapshots),
                input_tokens=sum(item.context_tokens for item in snapshots),
                peak_context_tokens=max((item.context_tokens for item in snapshots), default=0),
                peak_visible_bytes=max((item.visible_bytes for item in snapshots), default=0),
                compactions=sum(
                    event["event_type"] == "compact_boundary" for event in events[row["session_id"]]
                ),
                repeated_bytes=sum(item.repeated_bytes for item in snapshots),
                snapshots=snapshots,
                input_uncached=sum(s.input_uncached for s in snapshots),
                cache_read=sum(s.cache_read_tokens for s in snapshots),
                cache_write=sum(s.cache_creation_tokens for s in snapshots),
                output=sum(s.output_tokens for s in snapshots),
                growth=sorted(
                    (e for e in events[row["session_id"]] if e.get("byte_count")),
                    key=lambda e: int(e.get("byte_count") or 0),
                    reverse=True,
                )[:20],
            )
        )

    for summary in eligible:
        if pricing:
            costs = [
                pricing.cost(
                    s.model,
                    input_tokens=s.input_uncached,
                    output_tokens=s.output_tokens,
                    cache_read_tokens=s.cache_read_tokens,
                    cache_creation_tokens=s.cache_creation_tokens,
                )
                for s in summary.snapshots
            ]
            summary.cost = (
                sum(c for c in costs if c is not None)
                if all(c is not None for c in costs)
                else None
            )
            summary.cost_sort = summary.cost if summary.cost is not None else -1
        summary.stream_calls = {
            str(r["stream_id"]): int(r["calls"])
            for r in conn.execute(
                """WITH RECURSIVE tree(id) AS (
                    SELECT ? UNION SELECT s.session_id FROM sessions s JOIN tree t
                    ON s.parent_session_id=t.id
                ) SELECT ce.session_id || ':' || ce.stream_id AS stream_id, COUNT(*) AS calls
                FROM context_events ce JOIN tree t ON ce.session_id=t.id
                WHERE event_type='model_call' GROUP BY ce.session_id, ce.stream_id""",
                (summary.session_id,),
            )
        }
    matched_sessions = len(eligible)
    sort_field = _SORT_COLUMNS.get(flt.sort, "input_tokens")
    ranked = sorted(
        eligible, key=lambda item: (getattr(item, sort_field), item.session_id), reverse=True
    )
    population = (
        ranked[: flt.top_n]
        if flt.scope == "top"
        else [s for s in eligible if s.session_id in flt.selected_ids]
        if flt.scope == "selected"
        else eligible
    )
    by_provider: dict[str, list[SessionContextSummary]] = defaultdict(list)
    for summary in population:
        by_provider[summary.agent].append(summary)
    providers = [
        _provider_summary(agent, summaries) for agent, summaries in sorted(by_provider.items())
    ]
    sort_field = _SORT_COLUMNS.get(flt.sort, "input_tokens")
    ranked = sorted(
        eligible,
        key=lambda item: (getattr(item, sort_field), item.session_id),
        reverse=True,
    )
    selected = next(
        (item for item in eligible if item.session_id == flt.session_id),
        ranked[0] if ranked else None,
    )
    return ContextAnalysisReport(
        sessions=ranked[: max(1, min(flt.top_n, 100))],
        providers=providers,
        selected=selected,
        advice=_advisor(population),
        matched_sessions=matched_sessions,
        analyzed_ids=tuple(s.session_id for s in population),
        total_sessions=len(population),
        total_model_calls=sum(item.model_calls for item in population),
        total_compactions=sum(item.compactions for item in population),
    )
