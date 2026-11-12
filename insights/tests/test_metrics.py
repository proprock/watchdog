"""Tests for the metrics layer: filters, pricing, and report assembly."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.config import ModelPricing
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.metrics import queries
from wdi.metrics.compute import build_report
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver

MACHINE = "m1"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> Repository:
    repository = Repository(conn)
    repository.ensure_machine(MACHINE)
    return repository


def add_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    agent: str = "claude",
    model: str = "claude-opus-4-8",
    project: str = "demo",
    started: str = "2026-06-01T10:00:00Z",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 20,
    cache_creation: int | None = 10,
    reasoning: int | None = None,
    tool_calls: int = 0,
    duration_ms: int = 1000,
    context_window: int | None = None,
    task_duration_ms: int | None = None,
    time_to_first_token_ms: int | None = None,
    is_noise: bool = False,
    hook_count: int = 0,
    hook_error_count: int = 0,
    hook_commands: str | None = None,
    web_search: int | None = None,
    web_fetch: int | None = None,
    peak_context: int | None = None,
    turn_count: int = 0,
    turn_aborts: int = 0,
    rate_limit_primary: float | None = None,
    parent_session_id: str | None = None,
) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    project_id = repo.get_or_create_project(project)
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, project_id, agent, model, started_at, ended_at,
             duration_ms, input_tokens, output_tokens, cache_read_tokens,
             cache_creation_tokens, reasoning_tokens, tool_call_count,
             user_msg_count, assistant_msg_count, context_window, task_duration_ms,
             time_to_first_token_ms, is_noise, hook_count, hook_error_count,
             hook_commands, web_search_requests, web_fetch_requests,
             peak_context_tokens, turn_count, turn_abort_count,
             rate_limit_primary_used_percent, parent_session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            MACHINE,
            project_id,
            agent,
            model,
            started,
            started,
            duration_ms,
            input_tokens,
            output_tokens,
            cache_read,
            cache_creation,
            reasoning,
            tool_calls,
            context_window,
            task_duration_ms,
            time_to_first_token_ms,
            int(is_noise),
            hook_count,
            hook_error_count,
            hook_commands,
            web_search,
            web_fetch,
            peak_context,
            turn_count,
            turn_aborts,
            rate_limit_primary,
            parent_session_id,
        ),
    )
    conn.commit()


def add_tool_call(
    conn: sqlite3.Connection,
    session_id: str,
    idx: int,
    *,
    tool_name: str = "Read",
    category: str = "read",
    is_error: bool = False,
    stream_id: str = "main",
    target: str | None = None,
    command_kind: str | None = None,
    output_bytes: int | None = None,
    duration_ms: int | None = None,
    ts: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO tool_calls
            (session_id, machine_id, stream_id, idx, tool_name, category, is_error, target,
             command_kind, output_bytes, duration_ms, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            MACHINE,
            stream_id,
            idx,
            tool_name,
            category,
            int(is_error),
            target,
            command_kind,
            output_bytes,
            duration_ms,
            ts,
        ),
    )
    conn.commit()


def add_message(
    conn: sqlite3.Connection,
    session_id: str,
    idx: int,
    *,
    role: str = "assistant",
    ts: str | None = None,
    model: str | None = "claude-opus-4-8",
    input_tokens: int | None = None,
    cache_read: int | None = None,
    cache_creation: int | None = None,
    stop_reason: str | None = None,
    extra: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (session_id, machine_id, stream_id, idx, role, ts, model, input_tokens,
             cache_read_tokens, cache_creation_tokens, stop_reason, extra)
        VALUES (?, ?, 'main', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            MACHINE,
            idx,
            role,
            ts,
            model,
            input_tokens,
            cache_read,
            cache_creation,
            stop_reason,
            extra,
        ),
    )
    conn.commit()


def test_empty_db_report(conn: sqlite3.Connection) -> None:
    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert report.overview.sessions == 0
    assert report.overview.estimated_cost is None
    assert report.models == []
    assert report.tool_categories == []


def test_overview_and_cost(conn: sqlite3.Connection, repo: Repository) -> None:
    add_session(
        conn, "s1", input_tokens=1_000_000, output_tokens=1_000_000, cache_read=0, cache_creation=0
    )
    pricing = PricingResolver(
        {
            "claude-opus-4-8": ModelPricing(
                input=15.0, output=75.0, cache_read=1.5, cache_write=18.75
            )
        }
    )

    report = build_report(conn, SessionFilter(), pricing)

    assert report.overview.sessions == 1
    assert report.overview.claude_sessions == 1
    assert report.overview.total_tokens == 2_000_000
    assert report.overview.estimated_cost == pytest.approx(90.0)


def test_efficiency_overview_metrics(conn: sqlite3.Connection) -> None:
    add_session(
        conn,
        "s1",
        agent="codex",
        model="gpt-5.4",
        input_tokens=800,
        output_tokens=100,
        cache_read=200,
        cache_creation=None,
        reasoning=100,
        context_window=1000,
        task_duration_ms=5000,
        time_to_first_token_ms=250,
        peak_context=1000,
    )
    add_tool_call(conn, "s1", 0, category="edit", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", target="a.py")
    add_tool_call(conn, "s1", 2, category="read", target="a.py")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert report.overview.avg_time_to_first_token_ms == pytest.approx(250.0)
    assert report.overview.max_context_pressure == pytest.approx(1.0)
    assert report.overview.reasoning_ratio == pytest.approx(0.1)
    assert report.overview.tokens_per_edit == pytest.approx(1100.0)
    assert report.overview.repeated_reads == 1


def test_cost_none_for_unknown_model(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", model="mystery-model")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert report.overview.estimated_cost is None
    assert report.models[0].cost is None


def test_unpriced_models_list_token_totals(conn: sqlite3.Connection) -> None:
    add_session(
        conn,
        "s1",
        model="mystery-model",
        input_tokens=100,
        output_tokens=50,
        cache_read=20,
        cache_creation=10,
        reasoning=5,
    )
    add_session(
        conn,
        "s2",
        model="priced-model",
        input_tokens=1000,
        output_tokens=500,
        cache_read=0,
        cache_creation=0,
    )
    add_session(conn, "noise", model="noise-model", input_tokens=9999, is_noise=True)
    pricing = PricingResolver({"priced-model": ModelPricing(input=1.0, output=2.0)})

    report = build_report(conn, SessionFilter(), pricing)

    assert report.unpriced_models == [
        {
            "model": "mystery-model",
            "sessions": 1,
            "tokens": 185,
        }
    ]


def test_unpriced_models_empty_when_every_model_is_priced(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", model="priced-model")

    report = build_report(
        conn,
        SessionFilter(),
        PricingResolver({"priced-model": ModelPricing(input=1.0, output=2.0)}),
    )

    assert report.unpriced_models == []


def test_pricing_longest_prefix_match() -> None:
    pricing = PricingResolver(
        {
            "claude-haiku-4-5": ModelPricing(input=1.0, output=5.0),
            "claude": ModelPricing(input=99.0, output=99.0),
        }
    )

    resolved = pricing.resolve("claude-haiku-4-5-20251001")
    assert resolved is not None
    assert resolved.input == 1.0


def test_agent_filter(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", agent="claude")
    add_session(conn, "s2", agent="codex", model="gpt-5.4")

    claude = build_report(conn, SessionFilter(agent="claude"), PricingResolver({}))
    codex = build_report(conn, SessionFilter(agent="codex"), PricingResolver({}))

    assert claude.overview.sessions == 1
    assert codex.overview.sessions == 1


def test_project_filter(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", project="alpha")
    add_session(conn, "s2", project="beta")

    report = build_report(conn, SessionFilter(project="alpha"), PricingResolver({}))

    assert report.overview.sessions == 1
    assert report.projects[0].project == "alpha"


def test_since_filter_iso_and_relative(conn: sqlite3.Connection) -> None:
    add_session(conn, "old", started="2020-01-01T00:00:00Z")
    add_session(conn, "new", started="2026-06-10T00:00:00Z")

    iso = build_report(conn, SessionFilter(since="2026-01-01"), PricingResolver({}))
    assert iso.overview.sessions == 1

    relative = build_report(conn, SessionFilter(since="100000d"), PricingResolver({}))
    assert relative.overview.sessions == 2


def test_tool_categories_and_failing(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", tool_calls=3)
    add_tool_call(conn, "s1", 0, tool_name="Read", category="read")
    add_tool_call(conn, "s1", 1, tool_name="Bash", category="exec", is_error=True)
    add_tool_call(conn, "s1", 2, tool_name="Bash", category="exec", is_error=True)
    # Subagent-stream calls must not leak into main-stream tool metrics.
    add_tool_call(conn, "s1", 0, tool_name="Grep", category="search", stream_id="agentX")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    by_cat = {c.category: c for c in report.tool_categories}
    assert by_cat["exec"].calls == 2
    assert by_cat["exec"].errors == 2
    assert by_cat["exec"].error_rate == pytest.approx(1.0)
    assert "search" not in by_cat
    assert report.failing_tools[0].tool_name == "Bash"
    assert report.failing_tools[0].errors == 2


def test_shell_failure_clusters_by_command_kind(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    add_tool_call(
        conn,
        "s1",
        0,
        tool_name="shell_command",
        category="exec",
        is_error=True,
        command_kind="pytest",
    )
    add_tool_call(
        conn,
        "s1",
        1,
        tool_name="shell_command",
        category="exec",
        is_error=True,
        command_kind="pytest",
    )
    add_tool_call(
        conn,
        "s1",
        2,
        tool_name="shell_command",
        category="exec",
        is_error=True,
        command_kind="ruff",
    )

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert [(c.command_kind, c.errors) for c in report.shell_failure_clusters] == [
        ("pytest", 2),
        ("ruff", 1),
    ]


def test_subagent_efficiency_metrics(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    conn.execute(
        """
        INSERT INTO subagent_runs
            (agent_run_id, session_id, machine_id, agent_type, status,
             total_tokens, total_tool_use_count, read_count, search_count, edit_file_count)
        VALUES ('runA', 's1', ?, 'Explore', 'completed', 5000, 8, 3, 4, 0)
        """,
        (MACHINE,),
    )
    conn.commit()
    # Two reads of the same file in the run -> one repeated-read group.
    add_tool_call(conn, "s1", 0, category="read", stream_id="runA", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", stream_id="runA", target="a.py")
    add_tool_call(conn, "s1", 2, category="exec", stream_id="runA", is_error=True)

    report = build_report(conn, SessionFilter(agent="claude"), PricingResolver({}))

    assert len(report.subagents) == 1
    stat = report.subagents[0]
    assert stat.agent_type == "Explore"
    assert stat.runs == 1
    assert stat.repeated_reads == 1
    assert stat.trace_calls == 3
    assert stat.trace_errors == 1
    assert stat.search_to_edit is None  # zero edits


def test_oversearching_sessions(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    # search-heavy with a repeated read of the same target on the main stream.
    add_tool_call(conn, "s1", 0, category="read", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", target="a.py")
    add_tool_call(conn, "s1", 2, category="search")
    add_tool_call(conn, "s1", 3, category="search")
    add_tool_call(conn, "s1", 4, category="edit", target="a.py")
    # Subagent-stream calls must not leak into main-stream signals.
    add_tool_call(conn, "s1", 0, category="search", stream_id="runA")
    # A session with no search and no repeated reads is excluded.
    add_session(conn, "s2")
    add_tool_call(conn, "s2", 0, category="read", target="b.py")

    rows = queries.oversearching_sessions(conn, SessionFilter())

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["search_count"] == 2
    assert row["read_count"] == 2
    assert row["edit_count"] == 1
    assert row["repeated_reads"] == 1


def test_prompt_quality_reports(conn: sqlite3.Connection, repo: Repository) -> None:
    add_session(conn, "s1")
    add_session(conn, "s2")
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version="v1",
        model="claude-opus-4-8",
        score=7.0,
        result={"score": 7.0},
    )
    repo.upsert_llm_analysis(
        "s2",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version="v1",
        model="claude-opus-4-8",
        score=2.0,
        result={"score": 2.0},
    )
    # A different analysis type must not appear in the prompt-quality report.
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="session_summary",
        prompt_version="v1",
        model="claude-opus-4-8",
        score=None,
        result={"summary": "x"},
    )

    rows = queries.prompt_quality_reports(conn, SessionFilter())

    assert [r["session_id"] for r in rows] == ["s2", "s1"]  # worst score first
    assert rows[0]["score"] == 2.0


def test_session_list_signal_columns(conn: sqlite3.Connection, repo: Repository) -> None:
    add_session(conn, "s1")
    add_tool_call(conn, "s1", 0, category="read", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", target="a.py")  # repeated read
    add_tool_call(conn, "s1", 2, category="search")
    add_tool_call(conn, "s1", 3, category="edit", target="a.py")
    add_tool_call(conn, "s1", 4, category="exec", is_error=True)
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="session_summary",
        prompt_version="v1",
        model="m",
        score=88.0,
        result={"score": 88},
    )
    conn.commit()

    row = queries.session_list(conn, SessionFilter())[0]
    assert row["calls"] == 5
    assert row["errors"] == 1
    assert row["search_count"] == 1
    assert row["edit_count"] == 1
    assert row["repeated_reads"] == 1
    assert row["summary_score"] == 88.0
    assert row["first_prompt"] is None


def test_session_subagents_signal_columns(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    conn.execute(
        """
        INSERT INTO subagent_runs
            (agent_run_id, session_id, machine_id, agent_type, status,
             total_tokens, total_tool_use_count, search_count, edit_file_count)
        VALUES ('runA', 's1', ?, 'Explore', 'completed', 100, 4, 4, 0)
        """,
        (MACHINE,),
    )
    conn.commit()
    add_tool_call(conn, "s1", 0, category="read", stream_id="runA", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", stream_id="runA", target="a.py")
    add_tool_call(conn, "s1", 2, category="exec", stream_id="runA", is_error=True)

    run = queries.session_subagents(conn, "s1")[0]
    assert run["trace_calls"] == 3
    assert run["trace_errors"] == 1
    assert run["repeated_reads"] == 1
    assert run["search_count"] == 4
    assert run["edit_file_count"] == 0


def test_session_analyses_latest_per_type(conn: sqlite3.Connection, repo: Repository) -> None:
    add_session(conn, "s1")
    # Older prompt_quality version, then a newer one for the same type.
    insert = """
        INSERT INTO llm_analyses
            (session_id, machine_id, analysis_type, prompt_version,
             model, created_at, score, result)
        VALUES ('s1', ?, 'prompt_quality', ?, 'm', ?, ?, ?)
    """
    conn.execute(insert, (MACHINE, "v1", "2026-06-01T00:00:00Z", 40, '{"score": 40}'))
    conn.execute(insert, (MACHINE, "v2", "2026-06-02T00:00:00Z", 80, '{"score": 80}'))
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="session_summary",
        prompt_version="v1",
        model="m",
        score=55.0,
        result={"score": 55},
    )
    conn.commit()

    rows = queries.session_analyses(conn, "s1")

    by_type = {r["analysis_type"]: r for r in rows}
    assert set(by_type) == {"prompt_quality", "session_summary"}
    assert by_type["prompt_quality"]["score"] == 80  # newest version wins
    assert by_type["prompt_quality"]["prompt_version"] == "v2"


def test_noise_sessions_hidden_by_default(conn: sqlite3.Connection) -> None:
    add_session(conn, "real")
    add_session(conn, "observer", is_noise=True)

    default = build_report(conn, SessionFilter(), PricingResolver({}))
    included = build_report(conn, SessionFilter(include_noise=True), PricingResolver({}))

    assert default.overview.sessions == 1
    assert included.overview.sessions == 2


def test_waste_summary(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", web_search=3, web_fetch=1)
    add_tool_call(conn, "s1", 0, category="read", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", target="a.py")  # repeated read
    add_tool_call(conn, "s1", 2, category="exec", target="uv run pytest")
    add_tool_call(conn, "s1", 3, category="exec", target="uv run pytest")  # duplicate cmd
    add_tool_call(conn, "s1", 4, category="exec", is_error=True, output_bytes=2048)
    add_message(conn, "s1", 0, stop_reason="max_tokens")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    waste = report.waste
    assert waste is not None
    assert waste.repeated_reads == 1
    assert waste.duplicate_commands == 1
    assert waste.failed_calls == 1
    assert waste.failed_output_bytes == 2048
    assert waste.truncated_outputs == 1
    assert waste.web_search_requests == 3
    assert waste.web_fetch_requests == 1


def test_top_repeated_targets(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    add_session(conn, "s2")
    for idx in range(3):
        add_tool_call(conn, "s1", idx, category="read", target="hot.py")
    add_tool_call(conn, "s2", 0, category="read", target="hot.py")
    add_tool_call(conn, "s2", 1, category="read", target="hot.py")
    add_tool_call(conn, "s2", 2, category="read", target="once.py")

    rows = queries.top_repeated_targets(conn, SessionFilter())

    assert len(rows) == 1
    assert rows[0]["target"] == "hot.py"
    assert rows[0]["reads"] == 5
    assert rows[0]["wasted_reads"] == 3
    assert rows[0]["sessions"] == 2


def test_tool_detail_with_percentiles(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    for idx, duration in enumerate([100, 200, 300, 400, 10_000]):
        add_tool_call(
            conn,
            "s1",
            idx,
            tool_name="Bash",
            category="exec",
            duration_ms=duration,
            output_bytes=10,
            is_error=idx == 0,
        )

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    detail = next(d for d in report.tool_details if d.tool_name == "Bash")
    assert detail.calls == 5
    assert detail.errors == 1
    assert detail.output_bytes == 50
    assert detail.p50_ms == 300
    assert detail.p95_ms == 10_000
    assert detail.error_rate == pytest.approx(0.2)


def test_hook_stats(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", hook_count=2, hook_error_count=1, hook_commands="lint\ntest")
    add_session(conn, "s2", hook_count=1, hook_commands="lint")
    add_session(conn, "s3")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    hooks = report.hooks
    assert hooks is not None
    assert hooks.sessions_with_hooks == 2
    assert hooks.hook_count == 3
    assert hooks.hook_error_count == 1
    assert hooks.top_commands[0] == ("lint", 2)


def test_cache_trend_buckets(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", started="2026-06-01T10:00:00Z", input_tokens=100, cache_read=900)
    add_session(conn, "s2", started="2026-06-02T10:00:00Z", input_tokens=500, cache_read=500)

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    by_day = {b.period: b for b in report.cache_trend}
    assert by_day["2026-06-01"].hit_rate == pytest.approx(0.9)
    assert by_day["2026-06-02"].hit_rate == pytest.approx(0.5)


def test_cache_miss_attribution(conn: sqlite3.Connection) -> None:
    # cold: first turn writes a large cache from nothing.
    add_session(conn, "cold")
    add_message(conn, "cold", 0, ts="2026-06-01T10:00:00Z", cache_read=0, cache_creation=8000)

    # tools: TTL expired during a 10-minute gap dominated by a long tool call.
    add_session(conn, "tools")
    add_message(conn, "tools", 0, ts="2026-06-01T10:00:00Z", input_tokens=100, cache_read=8000)
    add_tool_call(conn, "tools", 0, ts="2026-06-01T10:00:30Z", duration_ms=360_000, category="exec")
    add_message(conn, "tools", 1, ts="2026-06-01T10:10:00Z", cache_read=0, cache_creation=8100)

    # idle: same gap but no tool activity -> the user was away.
    add_session(conn, "idle")
    add_message(conn, "idle", 0, ts="2026-06-01T10:00:00Z", input_tokens=100, cache_read=8000)
    add_message(conn, "idle", 1, ts="2026-06-01T10:10:00Z", cache_read=0, cache_creation=8100)

    # hit: cache read continues, no miss.
    add_session(conn, "hit")
    add_message(conn, "hit", 0, ts="2026-06-01T10:00:00Z", input_tokens=100, cache_read=8000)
    add_message(conn, "hit", 1, ts="2026-06-01T10:01:00Z", input_tokens=50, cache_read=8100)

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    misses = report.cache_misses
    assert misses is not None
    assert misses.cold_start == 1
    assert misses.tool_wait == 1
    assert misses.user_idle == 1
    assert misses.other == 0
    assert misses.total == 3
    assert misses.wasted_tokens == 8000 + 8100 + 8100


def test_subagent_opportunities(conn: sqlite3.Connection) -> None:
    add_session(conn, "heavy")
    for idx in range(21):
        category = "read" if idx % 2 else "search"
        add_tool_call(conn, "heavy", idx, category=category, output_bytes=100)
    # A session under the threshold and one that used subagents are excluded.
    add_session(conn, "light")
    add_tool_call(conn, "light", 0, category="read")
    add_session(conn, "delegated")
    for idx in range(25):
        add_tool_call(conn, "delegated", idx, category="search")
    conn.execute(
        """
        INSERT INTO subagent_runs (agent_run_id, session_id, machine_id, agent_type)
        VALUES ('runA', 'delegated', ?, 'Explore')
        """,
        (MACHINE,),
    )
    conn.commit()

    rows = queries.subagent_opportunities(conn, SessionFilter())

    assert [r["session_id"] for r in rows] == ["heavy"]
    assert rows[0]["search_count"] + rows[0]["read_count"] == 21
    assert rows[0]["lookup_output_bytes"] == 2100


def test_session_context_curve_with_compaction(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    add_message(conn, "s1", 0, ts="t0", input_tokens=100, cache_read=1000, cache_creation=200)
    add_message(
        conn,
        "s1",
        1,
        role="system",
        extra='{"subtype": "compact_boundary", "pre_tokens": 1300, "post_tokens": 400}',
    )
    add_message(conn, "s1", 2, ts="t2", input_tokens=50, cache_read=400)
    # User messages and other streams stay out of the curve.
    add_message(conn, "s1", 3, role="user")

    rows = queries.session_context_curve(conn, "s1")

    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert rows[0]["context_tokens"] == 1300
    assert rows[1]["subtype"] == "compact_boundary"
    assert rows[1]["pre_tokens"] == 1300
    assert rows[2]["context_tokens"] == 450


def test_session_waste_and_repeated_targets(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1")
    add_tool_call(conn, "s1", 0, category="read", target="a.py")
    add_tool_call(conn, "s1", 1, category="read", target="a.py")
    add_tool_call(conn, "s1", 2, category="exec", target="ls")
    add_tool_call(conn, "s1", 3, category="exec", target="ls")
    add_tool_call(conn, "s1", 4, category="exec", is_error=True, output_bytes=512)
    add_message(conn, "s1", 0, stop_reason="max_tokens")

    waste = queries.session_waste(conn, "s1")
    assert waste["repeated_reads"] == 1
    assert waste["duplicate_commands"] == 1
    assert waste["failed_calls"] == 1
    assert waste["failed_output_bytes"] == 512
    assert waste["truncated_outputs"] == 1

    targets = queries.session_repeated_targets(conn, "s1")
    assert [(t["target"], t["reads"]) for t in targets] == [("a.py", 2)]


def test_activity_buckets(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", started="2026-06-01T10:00:00Z")
    add_session(conn, "s2", started="2026-06-01T12:00:00Z")
    add_session(conn, "s3", started="2026-06-02T09:00:00Z")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    by_day = {b.period: b.sessions for b in report.activity}
    assert by_day["2026-06-01"] == 2
    assert by_day["2026-06-02"] == 1


def test_activity_per_agent_counts(conn: sqlite3.Connection) -> None:
    add_session(conn, "c1", agent="claude", started="2026-06-01T10:00:00Z")
    add_session(conn, "x1", agent="codex", model="gpt-5.4", started="2026-06-01T12:00:00Z")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    bucket = report.activity[0]
    assert bucket.claude_sessions == 1
    assert bucket.codex_sessions == 1


def test_context_pressure_uses_peak(conn: sqlite3.Connection) -> None:
    add_session(
        conn,
        "x1",
        agent="codex",
        model="gpt-5.4",
        input_tokens=500_000,
        cache_read=100_000,
        context_window=100_000,
        peak_context=42_000,
    )

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    # Session token totals exceed the window; pressure must use the peak.
    assert report.overview.max_context_pressure == pytest.approx(0.42)


def test_overview_turns_and_rate_limit(conn: sqlite3.Connection) -> None:
    add_session(
        conn,
        "x1",
        agent="codex",
        model="gpt-5.4",
        turn_count=5,
        turn_aborts=1,
        rate_limit_primary=42.5,
    )

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert report.overview.turns == 5
    assert report.overview.turn_aborts == 1
    assert report.overview.rate_limit_primary_max == pytest.approx(42.5)


def test_availability_flags(conn: sqlite3.Connection) -> None:
    add_session(conn, "c1", agent="claude", cache_creation=10, hook_count=2, web_search=1)
    add_message(conn, "c1", 0, stop_reason="max_tokens")
    add_session(
        conn,
        "x1",
        agent="codex",
        model="gpt-5.4",
        cache_creation=None,
        reasoning=5,
        context_window=1000,
        time_to_first_token_ms=100,
        turn_aborts=1,
        rate_limit_primary=10.0,
        peak_context=500,
    )

    mixed = build_report(conn, SessionFilter(), PricingResolver({})).availability
    assert mixed.claude and mixed.codex
    assert mixed.cache_creation and mixed.cache_misses and mixed.hooks
    assert mixed.web_requests and mixed.stop_reason
    assert mixed.reasoning and mixed.ttft and mixed.context_window
    assert mixed.rate_limits and mixed.turn_aborts

    codex_only = build_report(conn, SessionFilter(agent="codex"), PricingResolver({}))
    av = codex_only.availability
    assert av.codex and not av.claude
    assert not av.cache_creation and not av.hooks and not av.stop_reason
    assert av.reasoning and av.ttft
    assert codex_only.cache_misses is None
    assert codex_only.hooks is None

    claude_only = build_report(conn, SessionFilter(agent="claude"), PricingResolver({}))
    assert claude_only.availability.claude
    assert not claude_only.availability.reasoning
    assert claude_only.hooks is not None


def test_availability_mcp_flag(conn: sqlite3.Connection) -> None:
    add_session(conn, "x1", agent="codex", model="gpt-5.4")
    add_tool_call(conn, "x1", 0, tool_name="search_graph", category="mcp")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    assert report.availability.mcp


def test_waste_aborted_turns_and_truncated_gating(conn: sqlite3.Connection) -> None:
    add_session(conn, "c1", agent="claude")
    add_message(conn, "c1", 0, stop_reason="max_tokens")
    add_session(conn, "x1", agent="codex", model="gpt-5.4", turn_aborts=2)
    # A stray stop_reason on a codex row must not count as a truncated output.
    add_message(conn, "x1", 0, model="gpt-5.4", stop_reason="max_tokens")

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    waste = report.waste
    assert waste is not None
    assert waste.truncated_outputs == 1
    assert waste.aborted_turns == 2


def test_hook_summary_excludes_codex_rows(conn: sqlite3.Connection) -> None:
    add_session(conn, "c1", agent="claude", hook_count=2)
    add_session(conn, "x1", agent="codex", model="gpt-5.4", hook_count=3)

    report = build_report(conn, SessionFilter(), PricingResolver({}))

    hooks = report.hooks
    assert hooks is not None
    assert hooks.hook_count == 2


def test_session_context_curve_includes_codex_token_counts(conn: sqlite3.Connection) -> None:
    add_session(conn, "x1", agent="codex", model="gpt-5.4")
    # Codex assistant rows carry no usage and must stay off the curve.
    add_message(conn, "x1", 0, model="gpt-5.4")
    add_message(
        conn,
        "x1",
        1,
        role="event",
        model=None,
        input_tokens=900,
        cache_read=100,
        extra='{"subtype": "token_count"}',
    )

    rows = queries.session_context_curve(conn, "x1")

    assert [r["idx"] for r in rows] == [1]
    assert rows[0]["context_tokens"] == 1000


def test_project_names(conn: sqlite3.Connection) -> None:
    add_session(conn, "s1", project="beta")
    add_session(conn, "s2", project="alpha")

    assert queries.project_names(conn) == ["alpha", "beta"]
