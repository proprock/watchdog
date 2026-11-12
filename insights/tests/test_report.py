"""Tests for HTML report rendering."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.metrics.compute import build_report
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver
from wdi.report.html import render_report, report_payload

MACHINE = "m1"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    yield connection
    connection.close()


def _seed(conn: sqlite3.Connection, *, project: str = "demo") -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    project_id = repo.get_or_create_project(project)
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, project_id, agent, model, title, started_at, ended_at,
             duration_ms, input_tokens, output_tokens, cache_read_tokens,
             cache_creation_tokens, reasoning_tokens, context_window,
             time_to_first_token_ms, tool_call_count, user_msg_count, assistant_msg_count)
        VALUES ('s1', ?, ?, 'claude', 'claude-opus-4-8', 'Demo', '2026-06-01T10:00:00Z',
                '2026-06-01T10:05:00Z', 300000, 1000, 500, 200, 100, 50, 2000, 400,
                2, 1, 1)
        """,
        (MACHINE, project_id),
    )
    conn.execute(
        """
        INSERT INTO tool_calls (session_id, machine_id, stream_id, idx, tool_name, category)
        VALUES ('s1', ?, 'main', 0, 'Read', 'read'), ('s1', ?, 'main', 1, 'Bash', 'exec')
        """,
        (MACHINE, MACHINE),
    )
    conn.commit()


def _report(conn: sqlite3.Connection):
    return build_report(conn, SessionFilter(), PricingResolver({}))


def test_render_contains_data_and_assets(conn: sqlite3.Connection) -> None:
    _seed(conn)

    html = render_report(_report(conn))

    assert html.startswith("<!DOCTYPE html>")
    assert "Chart.js v4.4.6" in html  # vendored library is inlined
    assert 'id="report-data"' in html
    assert "demo" in html  # project name from the aggregate table
    assert "Tokens and cost by model" in html
    assert "Context pressure" in html
    assert "TTFT" in html
    assert "Tokens/edit" in html


def test_empty_report_renders_empty_state(conn: sqlite3.Connection) -> None:
    html = render_report(_report(conn))

    assert "No sessions match" in html
    assert "<canvas" not in html


def test_html_escapes_project_name(conn: sqlite3.Connection) -> None:
    _seed(conn, project="<script>alert(1)</script>")

    html = render_report(_report(conn))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_chart_data_island_has_no_unescaped_lt(conn: sqlite3.Connection) -> None:
    _seed(conn, project="</script><b>x</b>")

    html = render_report(_report(conn))
    start = html.index('id="report-data"')
    island = html[start : html.index("</script>", start)]

    # The payload must not contain a raw "<" that could break out of the script.
    assert "<" not in island.split(">", 1)[1]


def test_report_payload_shape(conn: sqlite3.Connection) -> None:
    _seed(conn)

    payload = report_payload(_report(conn))
    overview = payload["overview"]
    tool_categories = payload["tool_categories"]
    assert isinstance(overview, dict)
    assert isinstance(tool_categories, list)

    assert overview["sessions"] == 1
    assert payload["models"]
    assert {"category": "exec", "calls": 1, "errors": 0} in tool_categories
    assert payload["unpriced_models"] == [
        {"model": "claude-opus-4-8", "sessions": 1, "tokens": 1850}
    ]


def test_report_renders_unpriced_model_warning(conn: sqlite3.Connection) -> None:
    _seed(conn)

    html = render_report(_report(conn))

    assert "Unpriced models" in html
    assert "claude-opus-4-8" in html
    assert "cost totals are underestimated" in html


def test_chart_data_includes_cache_series(conn: sqlite3.Connection) -> None:
    from wdi.report.charts import chart_data

    _seed(conn)
    data = chart_data(_report(conn))

    assert "cache_trend" in data
    trend = data["cache_trend"]
    assert isinstance(trend, dict)
    assert trend["labels"]
    assert "cache_misses" in data


def _seed_codex(conn: sqlite3.Connection) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    project_id = repo.get_or_create_project("codex-demo")
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, project_id, agent, model, title, started_at, ended_at,
             duration_ms, input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
             context_window, peak_context_tokens, time_to_first_token_ms, turn_count,
             turn_abort_count, rate_limit_primary_used_percent, tool_call_count,
             user_msg_count, assistant_msg_count)
        VALUES ('x1', ?, ?, 'codex', 'gpt-5.4', 'Codex demo', '2026-06-02T10:00:00Z',
                '2026-06-02T10:05:00Z', 300000, 800, 100, 0, 100, 1000, 900, 250, 2, 1,
                40.0, 0, 1, 1)
        """,
        (MACHINE, project_id),
    )
    conn.commit()


def test_agent_only_labels_present(conn: sqlite3.Connection) -> None:
    _seed(conn)

    html = render_report(_report(conn))

    # The claude seed carries codex-style fields too (context window, TTFT),
    # so both label kinds must appear.
    assert "(codex only)" in html
    assert "(claude only)" in html


def test_codex_only_report_hides_claude_sections(conn: sqlite3.Connection) -> None:
    _seed_codex(conn)

    html = render_report(_report(conn))

    assert "Cache misses" not in html
    assert "Truncated outputs" not in html
    assert "Interrupted turns" in html
    assert "TTFT" in html


def test_chart_data_availability_and_agent_series(conn: sqlite3.Connection) -> None:
    from wdi.report.charts import chart_data

    _seed(conn)
    _seed_codex(conn)

    data = chart_data(_report(conn))

    availability = data["availability"]
    assert isinstance(availability, dict)
    assert availability["claude"] and availability["codex"]
    activity = data["activity"]
    assert isinstance(activity, dict)
    assert activity["claude"] == [1, 0]
    assert activity["codex"] == [0, 1]


def test_token_breakdown_drops_zero_categories(conn: sqlite3.Connection) -> None:
    from wdi.report.charts import chart_data

    _seed_codex(conn)  # cache_read = 0 -> the "Cache read" slice disappears

    data = chart_data(_report(conn))

    breakdown = data["token_breakdown"]
    assert isinstance(breakdown, dict)
    assert breakdown["labels"] == ["Input", "Output", "Reasoning"]
    assert breakdown["values"] == [800, 100, 100]


def test_session_chart_data_includes_codex_points(conn: sqlite3.Connection) -> None:
    from wdi.metrics import queries
    from wdi.report.charts import session_chart_data

    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    repo.ensure_session("x1", MACHINE, "codex")
    conn.execute(
        """
        INSERT INTO messages
            (session_id, machine_id, stream_id, idx, role, input_tokens,
             cache_read_tokens, extra)
        VALUES ('x1', ?, 'main', 0, 'event', 900, 100, '{"subtype": "token_count"}')
        """,
        (MACHINE,),
    )
    conn.commit()

    data = session_chart_data(queries.session_context_curve(conn, "x1"))

    curve = data["context_curve"]
    assert isinstance(curve, dict)
    assert curve["points"] == [{"idx": 0, "context": 1000, "output": 0}]
