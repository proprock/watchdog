"""Assemble metric reports from queries and pricing, reusable by CLI and HTML."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from wdi.metrics import queries
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver

# Anthropic prompt-cache default TTL; gaps beyond it force a cache re-write.
CACHE_TTL_MS = 5 * 60 * 1000
# A turn counts as a cache miss when reads drop below half of the previous
# context while at least that much is re-written; tiny contexts are skipped.
MISS_MIN_CONTEXT_TOKENS = 5_000


@dataclass(slots=True)
class AgentAvailability:
    """Which agent-specific metric groups have data under the current filter.

    Surfaces (CLI, static report, web) show a gated metric only when its flag
    is set, labeled "(claude only)" / "(codex only)".
    """

    claude: bool = False
    codex: bool = False
    # Claude-only signals
    cache_creation: bool = False
    cache_misses: bool = False
    hooks: bool = False
    web_requests: bool = False
    stop_reason: bool = False
    # Codex-only signals
    reasoning: bool = False
    ttft: bool = False
    context_window: bool = False
    rate_limits: bool = False
    turn_aborts: bool = False
    mcp: bool = False
    # Either agent
    subagents: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentAvailability:
        claude = bool(row["claude_sessions"])
        return cls(
            claude=claude,
            codex=bool(row["codex_sessions"]),
            cache_creation=bool(row["cache_creation_tokens"]),
            cache_misses=claude and bool(row["cache_creation_tokens"]),
            hooks=bool(row["hook_count"]),
            web_requests=bool(row["web_requests"]),
            stop_reason=bool(row["has_stop_reason"]),
            reasoning=bool(row["reasoning_tokens"]),
            ttft=bool(row["ttft_sessions"]),
            context_window=bool(row["context_window_sessions"]),
            rate_limits=bool(row["rate_limit_sessions"]),
            turn_aborts=bool(row["turn_aborts"]),
            mcp=bool(row["has_mcp"]),
            subagents=bool(row["has_subagents"]),
        )


@dataclass(slots=True)
class Overview:
    sessions: int
    claude_sessions: int
    codex_sessions: int
    projects: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    tool_calls: int
    total_duration_ms: int
    compactions: int
    api_errors: int
    avg_time_to_first_token_ms: float | None
    max_context_pressure: float | None
    edit_calls: int
    repeated_reads: int
    estimated_cost: float | None
    turns: int = 0
    turn_aborts: int = 0
    rate_limit_primary_max: float | None = None
    rate_limit_secondary_max: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        denom = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / denom if denom else None

    @property
    def reasoning_ratio(self) -> float | None:
        denom = self.input_tokens + self.cache_read_tokens
        return self.reasoning_tokens / denom if denom else None

    @property
    def tokens_per_edit(self) -> float | None:
        tokens = self.input_tokens + self.output_tokens + self.cache_read_tokens
        return tokens / self.edit_calls if self.edit_calls else None


@dataclass(slots=True)
class ModelStat:
    model: str
    sessions: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    cost: float | None


@dataclass(slots=True)
class ActivityBucket:
    period: str
    sessions: int
    tokens: int
    claude_sessions: int = 0
    codex_sessions: int = 0


@dataclass(slots=True)
class ToolCategoryStat:
    category: str
    calls: int
    errors: int

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


@dataclass(slots=True)
class FailingTool:
    tool_name: str
    calls: int
    errors: int


@dataclass(slots=True)
class ShellFailureCluster:
    command_kind: str
    calls: int
    errors: int


@dataclass(slots=True)
class ProjectStat:
    project: str
    sessions: int
    tokens: int
    tool_calls: int


@dataclass(slots=True)
class SubagentTypeStat:
    agent_type: str
    runs: int
    avg_tokens: float
    avg_tool_calls: float
    read_count: int
    search_count: int
    edit_count: int
    repeated_reads: int
    trace_errors: int
    trace_calls: int
    total_tokens: int = 0
    avg_duration_ms: float | None = None

    @property
    def search_to_edit(self) -> float | None:
        return self.search_count / self.edit_count if self.edit_count else None

    @property
    def trace_error_rate(self) -> float | None:
        return self.trace_errors / self.trace_calls if self.trace_calls else None

    @property
    def tokens_per_edit(self) -> float | None:
        return self.total_tokens / self.edit_count if self.edit_count else None


@dataclass(slots=True)
class CacheTrendBucket:
    period: str
    input_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def hit_rate(self) -> float | None:
        denom = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / denom if denom else None


@dataclass(slots=True)
class WasteStats:
    repeated_reads: int
    duplicate_commands: int
    failed_calls: int
    failed_output_bytes: int
    truncated_outputs: int
    web_search_requests: int
    web_fetch_requests: int
    aborted_turns: int = 0


@dataclass(slots=True)
class RepeatedTarget:
    target: str
    reads: int
    wasted_reads: int
    sessions: int


@dataclass(slots=True)
class ToolDetailStat:
    tool_name: str
    calls: int
    errors: int
    avg_duration_ms: float | None
    output_bytes: int
    p50_ms: int | None = None
    p95_ms: int | None = None

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


@dataclass(slots=True)
class HookStats:
    sessions_with_hooks: int
    hook_count: int
    hook_error_count: int
    hook_has_output_count: int
    top_commands: list[tuple[str, int]] = field(default_factory=list)


@dataclass(slots=True)
class CacheMissStats:
    """Cache misses attributed to their likely cause.

    tool_wait: the TTL expired while tools were running; user_idle: it expired
    while waiting for the user; cold_start: session began without a warm cache.
    """

    cold_start: int = 0
    tool_wait: int = 0
    user_idle: int = 0
    other: int = 0
    wasted_tokens_by_cause: dict[str, int] = field(default_factory=dict)
    estimated_cost: float | None = None

    @property
    def total(self) -> int:
        return self.cold_start + self.tool_wait + self.user_idle + self.other

    @property
    def wasted_tokens(self) -> int:
        return sum(self.wasted_tokens_by_cause.values())


@dataclass(slots=True)
class SubagentOpportunity:
    session_id: str
    title: str | None
    agent: str
    search_count: int
    read_count: int
    lookup_output_bytes: int


@dataclass(slots=True)
class MetricsReport:
    overview: Overview
    models: list[ModelStat]
    unpriced_models: list[dict[str, int | str]]
    activity: list[ActivityBucket]
    tool_categories: list[ToolCategoryStat]
    failing_tools: list[FailingTool]
    shell_failure_clusters: list[ShellFailureCluster]
    projects: list[ProjectStat]
    subagents: list[SubagentTypeStat] = field(default_factory=list)
    waste: WasteStats | None = None
    cache_trend: list[CacheTrendBucket] = field(default_factory=list)
    tool_details: list[ToolDetailStat] = field(default_factory=list)
    hooks: HookStats | None = None
    repeated_targets: list[RepeatedTarget] = field(default_factory=list)
    cache_misses: CacheMissStats | None = None
    subagent_opportunities: list[SubagentOpportunity] = field(default_factory=list)
    availability: AgentAvailability = field(default_factory=AgentAvailability)


def _model_cost(pricing: PricingResolver, row: sqlite3.Row) -> float | None:
    return pricing.cost(
        row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_creation_tokens=row["cache_creation_tokens"],
    )


def _model_tokens(row: sqlite3.Row) -> int:
    return (
        row["input_tokens"]
        + row["output_tokens"]
        + row["cache_read_tokens"]
        + row["cache_creation_tokens"]
        + row["reasoning_tokens"]
    )


def _percentile(sorted_values: list[int], fraction: float) -> int:
    return sorted_values[min(int(len(sorted_values) * fraction), len(sorted_values) - 1)]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tool_percentiles(
    conn: sqlite3.Connection, flt: SessionFilter, details: list[ToolDetailStat]
) -> None:
    durations: dict[str, list[int]] = defaultdict(list)
    for row in queries.tool_durations(conn, flt, [d.tool_name for d in details]):
        durations[row["tool_name"]].append(row["duration_ms"])
    for detail in details:
        values = sorted(durations.get(detail.tool_name, []))
        if values:
            detail.p50_ms = _percentile(values, 0.50)
            detail.p95_ms = _percentile(values, 0.95)


def _top_hook_commands(raw_lists: list[str], limit: int = 5) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for raw in raw_lists:
        counter.update(cmd for cmd in raw.split("\n") if cmd)
    return counter.most_common(limit)


def compute_cache_misses(
    conn: sqlite3.Connection, flt: SessionFilter, pricing: PricingResolver
) -> CacheMissStats:
    """Attribute Claude cache misses to their likely cause.

    A miss is a turn whose cache reads fall below half of the previous context
    while at least that much gets re-written (the TTL expired and the prefix was
    re-cached). The gap to the previous turn is then split between tool time and
    everything else (user think time).
    """
    tool_times: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for row in queries.tool_call_times(conn, flt):
        ts = _parse_ts(row["ts"])
        if ts is not None:
            tool_times[row["session_id"]].append((ts, row["duration_ms"]))

    stats = CacheMissStats()
    wasted: Counter[str] = Counter()
    wasted_by_model: Counter[str] = Counter()
    prev_ts: datetime | None = None
    prev_context = 0
    prev_session: str | None = None
    for row in queries.assistant_usage_rows(conn, flt):
        if row["session_id"] != prev_session:
            prev_ts = None
            prev_context = 0
            prev_session = row["session_id"]
        cache_read = row["cache_read_tokens"] or 0
        cache_creation = row["cache_creation_tokens"] or 0
        context = (row["input_tokens"] or 0) + cache_read + cache_creation
        cur_ts = _parse_ts(row["ts"])
        cause: str | None = None
        if prev_ts is None and prev_context == 0:
            if cache_read == 0 and cache_creation >= MISS_MIN_CONTEXT_TOKENS:
                cause = "cold_start"
        elif (
            prev_context >= MISS_MIN_CONTEXT_TOKENS
            and cache_read < 0.5 * prev_context
            and cache_creation >= 0.5 * prev_context
        ):
            cause = "other"
            if prev_ts is not None and cur_ts is not None:
                gap_ms = (cur_ts - prev_ts).total_seconds() * 1000
                if gap_ms > CACHE_TTL_MS:
                    tool_ms = sum(
                        duration
                        for ts, duration in tool_times.get(row["session_id"], [])
                        if prev_ts <= ts <= cur_ts
                    )
                    cause = "tool_wait" if tool_ms >= 0.5 * gap_ms else "user_idle"
        if cause is not None:
            setattr(stats, cause, getattr(stats, cause) + 1)
            wasted[cause] += cache_creation
            if row["model"]:
                wasted_by_model[row["model"]] += cache_creation
        prev_ts = cur_ts
        prev_context = context

    stats.wasted_tokens_by_cause = dict(wasted)
    if wasted_by_model:
        # Cost of re-writing the cache; a hit would have been ~10x cheaper reads.
        total = 0.0
        for model, tokens in wasted_by_model.items():
            cost = pricing.cost(
                model,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=tokens,
            )
            if cost is not None:
                total += cost
        stats.estimated_cost = total if total else None
    return stats


def _waste_stats(row: sqlite3.Row) -> WasteStats:
    return WasteStats(
        repeated_reads=row["repeated_reads"],
        duplicate_commands=row["duplicate_commands"],
        failed_calls=row["failed_calls"],
        failed_output_bytes=row["failed_output_bytes"],
        truncated_outputs=row["truncated_outputs"],
        web_search_requests=row["web_search_requests"],
        web_fetch_requests=row["web_fetch_requests"],
        aborted_turns=row["aborted_turns"],
    )


def _hook_stats(conn: sqlite3.Connection, flt: SessionFilter) -> HookStats:
    row = queries.hook_summary(conn, flt)
    return HookStats(
        sessions_with_hooks=row["sessions_with_hooks"] or 0,
        hook_count=row["hook_count"],
        hook_error_count=row["hook_error_count"],
        hook_has_output_count=row["hook_has_output_count"],
        top_commands=_top_hook_commands(queries.hook_commands(conn, flt)),
    )


def build_report(
    conn: sqlite3.Connection, flt: SessionFilter, pricing: PricingResolver
) -> MetricsReport:
    avail = AgentAvailability.from_row(queries.availability(conn, flt))
    tool_details = [
        ToolDetailStat(
            tool_name=r["tool_name"],
            calls=r["calls"],
            errors=r["errors"],
            avg_duration_ms=r["avg_duration_ms"],
            output_bytes=r["output_bytes"],
        )
        for r in queries.tool_detail(conn, flt)
    ]
    _tool_percentiles(conn, flt, tool_details)

    models: list[ModelStat] = []
    unpriced_models: list[dict[str, int | str]] = []
    total_cost: float | None = None
    for row in queries.tokens_by_model(conn, flt):
        cost = _model_cost(pricing, row)
        if cost is not None:
            total_cost = (total_cost or 0.0) + cost
        tokens = _model_tokens(row)
        if cost is None and row["model"] != "(unknown)" and tokens:
            unpriced_models.append(
                {
                    "model": row["model"],
                    "sessions": row["sessions"],
                    "tokens": tokens,
                }
            )
        models.append(
            ModelStat(
                model=row["model"],
                sessions=row["sessions"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_creation_tokens=row["cache_creation_tokens"],
                reasoning_tokens=row["reasoning_tokens"],
                cost=cost,
            )
        )

    ov = queries.overview(conn, flt)
    overview = Overview(
        sessions=ov["sessions"] or 0,
        claude_sessions=ov["claude_sessions"] or 0,
        codex_sessions=ov["codex_sessions"] or 0,
        projects=ov["projects"] or 0,
        input_tokens=ov["input_tokens"],
        output_tokens=ov["output_tokens"],
        cache_read_tokens=ov["cache_read_tokens"],
        cache_creation_tokens=ov["cache_creation_tokens"],
        reasoning_tokens=ov["reasoning_tokens"],
        tool_calls=ov["tool_calls"],
        total_duration_ms=ov["total_duration_ms"],
        compactions=ov["compactions"],
        api_errors=ov["api_errors"],
        avg_time_to_first_token_ms=ov["avg_time_to_first_token_ms"],
        max_context_pressure=ov["max_context_pressure"],
        edit_calls=ov["edit_calls"],
        repeated_reads=ov["repeated_reads"],
        estimated_cost=total_cost,
        turns=ov["turns"],
        turn_aborts=ov["turn_aborts"],
        rate_limit_primary_max=ov["rate_limit_primary_max"],
        rate_limit_secondary_max=ov["rate_limit_secondary_max"],
    )

    return MetricsReport(
        overview=overview,
        models=models,
        unpriced_models=unpriced_models,
        activity=[
            ActivityBucket(
                period=r["period"],
                sessions=r["sessions"],
                tokens=r["tokens"],
                claude_sessions=r["claude_sessions"] or 0,
                codex_sessions=r["codex_sessions"] or 0,
            )
            for r in queries.activity_by_period(conn, flt)
        ],
        tool_categories=[
            ToolCategoryStat(category=r["category"], calls=r["calls"], errors=r["errors"])
            for r in queries.tool_categories(conn, flt)
        ],
        failing_tools=[
            FailingTool(tool_name=r["tool_name"], calls=r["calls"], errors=r["errors"])
            for r in queries.top_failing_tools(conn, flt)
        ],
        shell_failure_clusters=[
            ShellFailureCluster(
                command_kind=r["command_kind"], calls=r["calls"], errors=r["errors"]
            )
            for r in queries.shell_failure_clusters(conn, flt)
        ],
        projects=[
            ProjectStat(
                project=r["project"],
                sessions=r["sessions"],
                tokens=r["tokens"],
                tool_calls=r["tool_calls"],
            )
            for r in queries.projects(conn, flt)
        ],
        subagents=[
            SubagentTypeStat(
                agent_type=r["agent_type"],
                runs=r["runs"],
                avg_tokens=r["avg_tokens"] or 0.0,
                avg_tool_calls=r["avg_tool_calls"] or 0.0,
                read_count=r["read_count"],
                search_count=r["search_count"],
                edit_count=r["edit_count"],
                repeated_reads=r["repeated_reads"],
                trace_errors=r["trace_errors"],
                trace_calls=r["trace_calls"],
                total_tokens=r["total_tokens"],
                avg_duration_ms=r["avg_duration_ms"],
            )
            for r in queries.subagent_types(conn, flt)
        ],
        waste=_waste_stats(queries.waste_summary(conn, flt)),
        cache_trend=[
            CacheTrendBucket(
                period=r["period"],
                input_tokens=r["input_tokens"],
                cache_read_tokens=r["cache_read_tokens"],
                cache_creation_tokens=r["cache_creation_tokens"],
            )
            for r in queries.cache_trend(conn, flt)
        ],
        tool_details=tool_details,
        hooks=_hook_stats(conn, flt) if avail.claude else None,
        repeated_targets=[
            RepeatedTarget(
                target=r["target"],
                reads=r["reads"],
                wasted_reads=r["wasted_reads"],
                sessions=r["sessions"],
            )
            for r in queries.top_repeated_targets(conn, flt)
        ],
        cache_misses=compute_cache_misses(conn, flt, pricing) if avail.claude else None,
        subagent_opportunities=[
            SubagentOpportunity(
                session_id=r["session_id"],
                title=r["title"],
                agent=r["agent"],
                search_count=r["search_count"],
                read_count=r["read_count"],
                lookup_output_bytes=r["lookup_output_bytes"],
            )
            for r in queries.subagent_opportunities(conn, flt)
        ],
        availability=avail,
    )
