"""Tests for the FastAPI web server (`wdi serve`)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wdi.config import Settings
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.web import create_app

MACHINE = "m1"


def _seed(conn: sqlite3.Connection, *, agent: str = "claude", project: str = "demo") -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    project_id = repo.get_or_create_project(project)
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, project_id, agent, model, title, started_at, ended_at,
             duration_ms, input_tokens, output_tokens, cache_read_tokens,
             cache_creation_tokens, tool_call_count, user_msg_count, assistant_msg_count)
        VALUES ('s1', ?, ?, ?, 'claude-opus-4-8', 'Demo session', '2026-06-01T10:00:00Z',
                '2026-06-01T10:05:00Z', 300000, 1000, 500, 200, 100, 2, 1, 1)
        """,
        (MACHINE, project_id, agent),
    )
    conn.execute(
        """
        INSERT INTO tool_calls (session_id, machine_id, stream_id, idx, tool_name, category)
        VALUES ('s1', ?, 'main', 0, 'Read', 'read'), ('s1', ?, 'main', 1, 'Bash', 'exec')
        """,
        (MACHINE, MACHINE),
    )
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version="v1",
        model="claude-opus-4-8",
        score=72.0,
        result={"score": 72, "summary": "Mostly clear prompts.", "advice": ["Add file paths."]},
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wdi.sqlite"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(machine_id=MACHINE, db_path=db_path)


@pytest.fixture
def seeded(db_path: Path) -> Iterator[None]:
    conn = connect(db_path)
    _seed(conn)
    conn.close()
    yield


@pytest.fixture
def empty_db(db_path: Path) -> Iterator[None]:
    conn = connect(db_path)
    conn.close()
    yield


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_dashboard_full_page(client: TestClient, seeded: None) -> None:
    res = client.get("/")

    assert res.status_code == 200
    assert "<!DOCTYPE html>" in res.text
    assert 'id="report-data"' in res.text
    assert "Demo session" not in res.text  # dashboard shows aggregates, not titles
    assert "demo" in res.text  # project aggregate row


def test_dashboard_initializes_new_database(client: TestClient) -> None:
    assert client.get("/").status_code == 200


def test_dashboard_empty_state(client: TestClient, empty_db: None) -> None:
    res = client.get("/")

    assert res.status_code == 200
    assert "No sessions match" in res.text
    assert "<canvas" not in res.text


def test_dashboard_htmx_returns_partial_only(client: TestClient, seeded: None) -> None:
    res = client.get("/", headers={"HX-Request": "true"})

    assert res.status_code == 200
    assert "<!DOCTYPE html>" not in res.text  # fragment, not a full page
    assert 'id="report-data"' in res.text


def test_dashboard_bookmarkable_filter(client: TestClient, seeded: None) -> None:
    res = client.get("/", params={"agent": "claude", "since": "7d"})
    assert res.status_code == 200

    other = client.get("/", params={"agent": "codex"})
    assert other.status_code == 200
    assert "No sessions match" in other.text  # only a claude session is seeded


def _seed_context_events(conn: sqlite3.Connection, session_id: str = "s1") -> None:
    conn.executemany(
        """
        INSERT INTO context_events
            (session_id, machine_id, stream_id, idx, model_call_index, event_type,
             category, byte_count, fingerprint, input_tokens, cache_read_tokens,
             context_window, compaction_generation, coverage, extra)
        VALUES (?, ?, 'main', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'visible', ?)
        """,
        [
            (
                session_id,
                MACHINE,
                0,
                0,
                "message",
                "user_prompt",
                120,
                "prompt-hash",
                None,
                None,
                None,
                '{"direction":"input"}',
            ),
            (
                session_id,
                MACHINE,
                1,
                0,
                "model_call",
                "usage",
                None,
                None,
                1000,
                200,
                10_000,
                None,
            ),
        ],
    )
    conn.commit()


def test_context_analysis_requires_explicit_run(client: TestClient, seeded: None) -> None:
    res = client.get("/context-analysis")

    assert res.status_code == 200
    assert "Run local analysis" in res.text
    assert "Choose filters" in res.text
    assert "Longest sessions" not in res.text
    assert 'href="/context-analysis"' in res.text


def test_context_analysis_results_and_htmx_fragment(
    client: TestClient, db_path: Path, seeded: None
) -> None:
    conn = connect(db_path)
    _seed_context_events(conn)
    conn.close()

    res = client.get(
        "/context-analysis",
        params={"run": "true", "agent": "claude", "top_n": 5, "sort": "model_calls"},
        headers={"HX-Request": "true"},
    )

    assert res.status_code == 200
    assert "<!DOCTYPE html>" not in res.text
    assert "Longest sessions" in res.text
    assert "Demo session" in res.text
    assert 'id="context-analysis-data"' in res.text
    assert "Per-call timeline" in res.text
    assert "insufficient evidence" in res.text


def test_context_analysis_empty_and_invalid_filters(client: TestClient, empty_db: None) -> None:
    empty = client.get("/context-analysis", params={"run": "true"})
    invalid = client.get("/context-analysis", params={"run": "true", "top_n": 101})

    assert empty.status_code == 200
    assert "No imported context events" in empty.text
    assert invalid.status_code == 422


def test_context_analysis_llm_advice_is_explicit_and_cached(
    client: TestClient,
    db_path: Path,
    seeded: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wdi.web.routes import context_analysis as context_route

    conn = connect(db_path)
    _seed_context_events(conn)
    conn.close()
    prompts: list[str] = []

    class Fake:
        def complete(self, prompt: str, model: str) -> str:
            prompts.append(prompt)
            assert model == "haiku"
            return json.dumps(
                {
                    "summary": "Compact at milestones.",
                    "advice": ["Preserve decisions."],
                    "focus_areas": ["recovery"],
                }
            )

    monkeypatch.setattr(context_route, "ClaudeCliBackend", Fake)
    page = client.get("/context-analysis", params={"run": "true"})
    first = client.post(
        "/context-analysis/advice/claude?run=1",
        data={"model": "haiku"},
    )
    cached = client.post(
        "/context-analysis/advice/claude?run=1",
        data={"model": "haiku"},
    )

    assert "Get advice" in page.text
    assert "Compact at milestones." in first.text
    assert "cached" in cached.text
    assert len(prompts) == 1
    assert "Demo session" not in prompts[0]
    assert "s1" not in prompts[0]


def test_invalid_agent_rejected(client: TestClient, seeded: None) -> None:
    res = client.get("/", params={"agent": "bogus"})
    assert res.status_code == 400


def test_invalid_since_does_not_crash(client: TestClient, seeded: None) -> None:
    res = client.get("/", params={"since": "not-a-date"})
    assert res.status_code == 200


def test_sessions_list(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions")

    assert res.status_code == 200
    assert "Demo session" in res.text
    assert "/sessions/s1" in res.text


def test_sessions_list_htmx_partial(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions", headers={"HX-Request": "true"})

    assert res.status_code == 200
    assert "<!DOCTYPE html>" not in res.text
    assert "Demo session" in res.text


def test_sessions_list_falls_back_to_first_prompt(
    client: TestClient, db_path: Path, seeded: None
) -> None:
    # Untitled session with a first user prompt -> name comes from the prompt.
    conn = connect(db_path)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, started_at)
        VALUES ('s2', ?, 'claude', '2026-06-03T10:00:00Z')
        """,
        (MACHINE,),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, machine_id, stream_id, idx, role, content_preview)
        VALUES ('s2', ?, 'main', 0, 'user', 'Investigate the failing migration')
        """,
        (MACHINE,),
    )
    conn.commit()
    conn.close()

    res = client.get("/sessions")
    assert res.status_code == 200
    assert "Investigate the failing migration" in res.text


def test_sessions_list_shows_badges_and_legend(
    client: TestClient, db_path: Path, seeded: None
) -> None:
    # Session with a high tool error rate -> error-prone badge.
    conn = connect(db_path)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, title, started_at)
        VALUES ('s3', ?, 'claude', 'Flaky session', '2026-06-04T10:00:00Z')
        """,
        (MACHINE,),
    )
    conn.executemany(
        """
        INSERT INTO tool_calls
            (session_id, machine_id, stream_id, idx, tool_name, category, is_error)
        VALUES ('s3', ?, 'main', ?, 'Bash', 'exec', ?)
        """,
        [(MACHINE, 0, 1), (MACHINE, 1, 1), (MACHINE, 2, 0)],
    )
    conn.commit()
    conn.close()

    res = client.get("/sessions")
    assert res.status_code == 200
    assert "Badge legend" in res.text
    assert "🔥" in res.text


def test_session_detail(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions/s1")

    assert res.status_code == 200
    assert "s1" in res.text
    assert "Tools" in res.text


def test_session_detail_shows_llm_analysis(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions/s1")

    assert res.status_code == 200
    assert "LLM analysis" in res.text
    assert "Prompt quality" in res.text
    assert "score 72" in res.text
    assert "Mostly clear prompts." in res.text
    assert "Add file paths." in res.text


def test_session_detail_no_analysis_hides_section(
    client: TestClient, db_path: Path, seeded: None
) -> None:
    # Add a second session that has no stored analysis.
    conn = connect(db_path)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, model, title, started_at)
        VALUES ('s2', ?, 'claude', 'claude-opus-4-8', 'Bare session', '2026-06-02T10:00:00Z')
        """,
        (MACHINE,),
    )
    conn.commit()
    conn.close()

    res = client.get("/sessions/s2")
    assert res.status_code == 200
    assert "LLM analysis" not in res.text


def _seed_session_with_source(conn: sqlite3.Connection, src: Path) -> None:
    """Insert a session whose transcript on disk holds one Read tool call (call-9)."""
    records = [
        {"type": "user", "message": {"role": "user", "content": "Inspect a.py"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-9",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-9",
                        "is_error": False,
                        "content": "FULL FILE OUTPUT " + "y" * 800,
                    },
                ],
            },
        },
    ]
    src.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, title, started_at, source_path)
        VALUES ('s9', ?, 'claude', 'Sourced session', '2026-06-05T10:00:00Z', ?)
        """,
        (MACHINE, str(src)),
    )
    conn.execute(
        """
        INSERT INTO tool_calls
            (session_id, machine_id, stream_id, idx, call_id, tool_name, category,
             input_preview, output_preview)
        VALUES ('s9', ?, 'main', 0, 'call-9', 'Read', 'read', '{"file_path": "a.py"}', 'short out')
        """,
        (MACHINE,),
    )
    conn.commit()


def test_stream_drilldown_lists_tool_calls(
    client: TestClient, db_path: Path, tmp_path: Path, seeded: None
) -> None:
    conn = connect(db_path)
    _seed_session_with_source(conn, tmp_path / "s9.jsonl")
    conn.close()

    res = client.get("/sessions/s9/stream/main")
    assert res.status_code == 200
    assert "Read" in res.text
    assert "Full I/O" in res.text

    partial = client.get("/sessions/s9/stream/main", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "<!DOCTYPE html>" not in partial.text


def test_tool_call_full_reads_source(
    client: TestClient, db_path: Path, tmp_path: Path, seeded: None
) -> None:
    conn = connect(db_path)
    _seed_session_with_source(conn, tmp_path / "s9.jsonl")
    conn.close()

    res = client.get("/sessions/s9/stream/main/call/call-9/full")
    assert res.status_code == 200
    assert "FULL FILE OUTPUT" in res.text  # full output, not the stored preview
    assert "Full source unavailable" not in res.text


def test_tool_call_full_falls_back_to_preview(client: TestClient, seeded: None) -> None:
    # Seeded session s1 has tool calls without a source file -> preview fallback.
    res = client.get("/sessions/s1/stream/main/call/none/full")
    assert res.status_code == 200
    # No matching call_id and no source -> the unavailable note is shown.
    assert "Full source unavailable" in res.text


def test_session_detail_unknown_id_404(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions/does-not-exist")

    assert res.status_code == 404
    assert "Session not found" in res.text


def test_static_assets_served(client: TestClient, seeded: None) -> None:
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/vendor/chart.umd.min.js").status_code == 200


def test_noise_sessions_hidden_and_toggle(client: TestClient, db_path: Path) -> None:
    conn = connect(db_path)
    _seed(conn)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, title, started_at, is_noise)
        VALUES ('obs1', ?, 'claude', 'Observer run', '2026-06-01T11:00:00Z', 1)
        """,
        (MACHINE,),
    )
    conn.commit()
    conn.close()

    hidden = client.get("/sessions")
    shown = client.get("/sessions", params={"include_noise": "1"})

    assert "Observer run" not in hidden.text
    assert "Observer run" in shown.text
    # The filter form exposes the toggle.
    assert "include_noise" in hidden.text


def test_tools_page_and_drilldown(client: TestClient, seeded: None) -> None:
    page = client.get("/tools")
    assert page.status_code == 200
    assert "Read" in page.text
    assert "Bash" in page.text

    partial = client.get("/tools", headers={"HX-Request": "true"})
    assert "<!DOCTYPE html>" not in partial.text
    assert "Read" in partial.text

    detail = client.get("/tools/Read")
    assert detail.status_code == 200
    assert "Demo session" in detail.text


def test_session_detail_has_context_chart_island(client: TestClient, seeded: None) -> None:
    res = client.get("/sessions/s1")

    assert res.status_code == 200
    assert 'id="session-chart-data"' in res.text
    assert "context_curve" in res.text


def test_dashboard_shows_waste_cards(client: TestClient, seeded: None) -> None:
    res = client.get("/")

    assert res.status_code == 200
    assert "Repeated reads" in res.text
    assert "Failed tool calls" in res.text


def test_coach_button_on_dashboard(client: TestClient, seeded: None) -> None:
    res = client.get("/")

    assert res.status_code == 200
    assert 'id="coach"' in res.text
    assert "Get Claude advice" in res.text
    assert "Get Codex advice" in res.text
    assert 'id="coach-claude-model"' in res.text
    assert 'id="coach-codex-model"' in res.text


def test_coach_hidden_when_llm_disabled(db_path: Path, seeded: None) -> None:
    settings = Settings(machine_id=MACHINE, db_path=db_path)
    settings.features.llm_analysis.enabled = False
    client = TestClient(create_app(settings))

    res = client.get("/")

    assert res.status_code == 200
    assert 'id="coach"' not in res.text


def test_coach_post_renders_cached_advice(
    client: TestClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdi.web.routes import coach as coach_route

    calls: list[str] = []

    class Fake:
        def complete(self, prompt: str, model: str) -> str:
            calls.append(model)
            return json.dumps({"summary": "Looks fine.", "advice": ["Keep the cache warm."]})

    monkeypatch.setattr(coach_route, "ClaudeCliBackend", Fake)
    first = client.post("/coach/claude", data={"model": "haiku"})
    res = client.post("/coach/claude", data={"model": "haiku"})

    assert first.status_code == res.status_code == 200
    assert calls == ["haiku"]
    assert "Looks fine." in res.text
    assert "Keep the cache warm." in res.text
    assert "cached" in res.text
    assert "Refresh Claude advice" in res.text
    assert 'id="coach-codex"' not in res.text


def test_coach_post_error_state_renders_retry(
    client: TestClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdi.llm.backend import LLMError
    from wdi.web.routes import coach as coach_route

    class Failing:
        def complete(self, prompt: str, model: str) -> str:
            raise LLMError("claude CLI not found")

    monkeypatch.setattr(coach_route, "ClaudeCliBackend", Failing)

    res = client.post("/coach/claude", data={"model": "haiku"})

    assert res.status_code == 200
    assert "Claude could not produce advice" in res.text
    assert "Retry" in res.text


def test_coach_providers_run_independently_and_preserve_filters(
    client: TestClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdi.web.routes import coach as coach_route

    calls: list[tuple[str, str]] = []

    class Claude:
        def complete(self, prompt: str, model: str) -> str:
            calls.append(("claude", model))
            return '{"summary": "Claude result"}'

    class Codex:
        def __init__(self, *, executable: str) -> None:
            assert executable == "codex"

        def complete(self, prompt: str, model: str) -> str:
            calls.append(("codex", model))
            return '{"summary": "Codex result"}'

    monkeypatch.setattr(coach_route, "ClaudeCliBackend", Claude)
    monkeypatch.setattr(coach_route, "CodexCliBackend", Codex)
    for provider, model in [("claude", "sonnet"), ("codex", "gpt-5.6-terra")]:
        res = client.post(
            f"/coach/{provider}?project=demo&agent=claude",
            data={"model": model},
            headers={"HX-Request": "true"},
        )
        assert res.status_code == 200
        assert f'id="coach-{provider}"' in res.text
        assert f'value="{model}" selected' in res.text
        assert "project=demo" in res.text
        assert "<!DOCTYPE" not in res.text
    assert calls == [("claude", "sonnet"), ("codex", "gpt-5.6-terra")]
    cached = client.get("/coach/codex?project=demo&agent=claude&model=gpt-5.6-terra")
    assert "Codex result" in cached.text
    assert "Claude result" not in cached.text
    assert len(calls) == 2


def test_coach_model_selection_and_invalid_input_never_call_backend(
    client: TestClient, seeded: None, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from wdi.web.routes import coach as coach_route

    def unexpected():
        pytest.fail("Backend must not be created")

    monkeypatch.setattr(coach_route, "CodexCliBackend", unexpected)
    selected = client.get("/coach/codex?model=gpt-5.6-luna")
    assert 'value="gpt-5.6-luna" selected' in selected.text
    assert "No advice yet" in selected.text
    invalid = client.post("/coach/codex", data={"model": "not-configured"})
    assert "Choose a model" in invalid.text
    assert client.post("/coach/unknown", data={"model": "haiku"}).status_code == 422
    assert client.post("/coach/codex").status_code == 422
    settings.features.llm_analysis.enabled = False
    disabled = client.post("/coach/codex", data={"model": "gpt-5.6-sol"})
    assert "disabled" in disabled.text


def _seed_codex_session(
    conn: sqlite3.Connection,
    session_id: str = "x1",
    *,
    parent: str | None = None,
) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, agent, model, title, started_at, ended_at, duration_ms,
             input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, context_window,
             peak_context_tokens, time_to_first_token_ms, task_duration_ms, turn_count,
             turn_abort_count, rate_limit_primary_used_percent, rate_limit_plan, effort,
             approval_policy, sandbox_policy_type, parent_session_id,
             tool_call_count, user_msg_count, assistant_msg_count, is_noise)
        VALUES (?, ?, 'codex', 'gpt-5.4', 'Codex run', '2026-06-02T10:00:00Z',
                '2026-06-02T10:05:00Z', 300000, 800, 100, 100, 40, 10000, 1000, 250, 9000,
                3, 1, 40.0, 'plus', 'high', 'on-request', 'workspace-write', ?, 0, 1, 1, ?)
        """,
        (session_id, MACHINE, parent, 1 if parent else 0),
    )
    conn.execute(
        """
        INSERT INTO messages
            (session_id, machine_id, stream_id, idx, role, input_tokens,
             cache_read_tokens, extra)
        VALUES (?, ?, 'main', 0, 'event', 900, 100, '{"subtype": "token_count"}')
        """,
        (session_id, MACHINE),
    )
    conn.commit()


def test_filters_have_period_and_project_dropdowns(client: TestClient, seeded: None) -> None:
    res = client.get("/")

    assert res.status_code == 200
    assert '<select name="since">' in res.text
    assert 'value="7d"' in res.text
    assert 'value="365d"' in res.text
    assert '<select name="project">' in res.text
    assert 'value="demo"' in res.text


def test_since_custom_value_preserved_in_dropdown(client: TestClient, seeded: None) -> None:
    res = client.get("/", params={"since": "2026-06-01"})

    assert res.status_code == 200
    assert "Custom: 2026-06-01" in res.text


def test_codex_session_detail_rows_and_chart(
    client: TestClient, db_path: Path, seeded: None
) -> None:
    conn = connect(db_path)
    _seed_codex_session(conn)
    conn.close()

    res = client.get("/sessions/x1")

    assert res.status_code == 200
    assert "Turns / interrupted" in res.text
    assert "high" in res.text  # effort row
    assert "plus" in res.text  # rate limit plan
    assert "Context growth" in res.text  # curve exists -> chart section rendered


def test_codex_parent_child_links(client: TestClient, db_path: Path, seeded: None) -> None:
    conn = connect(db_path)
    _seed_codex_session(conn, "p1")
    _seed_codex_session(conn, "x2", parent="p1")
    conn.execute(
        """
        INSERT INTO subagent_runs
            (agent_run_id, session_id, machine_id, agent_type, total_tokens,
             total_tool_use_count)
        VALUES ('x2', 'p1', ?, 'guardian', 1000, 1)
        """,
        (MACHINE,),
    )
    conn.commit()
    conn.close()

    parent_page = client.get("/sessions/p1")
    assert parent_page.status_code == 200
    assert "guardian" in parent_page.text
    assert "/sessions/x2" in parent_page.text  # codex subagent links to the child session

    child_page = client.get("/sessions/x2")
    assert child_page.status_code == 200
    assert "/sessions/p1" in child_page.text  # child links back to the parent


def test_claude_session_without_curve_hides_chart(client: TestClient, seeded: None) -> None:
    # Seeded s1 has no usage-bearing messages -> no curve -> no chart section.
    res = client.get("/sessions/s1")

    assert res.status_code == 200
    assert "Context growth" not in res.text


def test_codex_only_dashboard_hides_claude_cards(client: TestClient, db_path: Path) -> None:
    conn = connect(db_path)
    _seed_codex_session(conn)
    conn.close()

    res = client.get("/")

    assert res.status_code == 200
    assert "(codex only)" in res.text
    assert "Interrupted turns" in res.text
    assert "Cache misses" not in res.text
    assert "Truncated outputs" not in res.text


def test_context_selection_estimate_and_scenarios(client: TestClient, seeded: None, db_path: Path):
    conn = connect(db_path)
    _seed_context_events(conn)
    conn.close()
    response = client.get(
        "/context-analysis",
        params={
            "run": 1,
            "agent": "",
            "scope": "selected",
            "selected_ids": "s1",
            "estimate_provider": "codex",
            "model": "gpt-5.6-sol",
            "output_budget": 900,
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "1 analyzed (selected)" in response.text
    assert "Approximate input" in response.text
    assert "output: 900" in response.text
    assert "<!DOCTYPE" not in response.text
    unknown = client.get("/context-analysis/scenarios?model=not-priced&calls=3")
    assert unknown.status_code == 200
    assert "Unknown tariff" in unknown.text
    assert "Earlier native compact" in unknown.text
    assert client.get("/context-analysis/scenarios?cache_hit=2").status_code == 422
    assert client.get("/context-analysis?scope=other").status_code == 422


def test_context_profiles_do_not_launch_an_agent(client: TestClient, monkeypatch):
    from wdi import context_profiles

    monkeypatch.setattr(context_profiles, "detect_codex_keys", lambda: set())
    response = client.get("/context-analysis/profiles")
    assert response.status_code == 200
    assert "Not verified" in response.text
