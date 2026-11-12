"""Tests for the LLM analysis layer using a fake backend (no real subprocess)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.config import ModelPricing
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.llm.analyzers import ANALYZERS, _parse_response
from wdi.llm.backend import LLMError
from wdi.llm.service import estimate_analyzer, resolve_analyzers, run_analyzer
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver

MACHINE = "m1"


class FakeBackend:
    def __init__(self, response: str = '{"score": 80, "summary": "ok", "advice": ["x"]}') -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        return self.response


class FailingBackend:
    def complete(self, prompt: str, model: str) -> str:
        raise LLMError("backend down")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    yield connection
    connection.close()


def _session(
    conn: sqlite3.Connection, session_id: str = "s1", *, with_subagent: bool = False
) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    pid = repo.get_or_create_project("demo")
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, project_id, agent, model, started_at,
                              user_msg_count, assistant_msg_count, tool_call_count)
        VALUES (?, ?, ?, 'claude', 'claude-opus-4-8', '2026-06-01T10:00:00Z', 1, 1, 0)
        """,
        (session_id, MACHINE, pid),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, machine_id, stream_id, idx, role, content_preview)
        VALUES (?, ?, 'main', 0, 'user', 'add a feature please')
        """,
        (session_id, MACHINE),
    )
    if with_subagent:
        conn.execute(
            """
            INSERT INTO subagent_runs (agent_run_id, session_id, machine_id, agent_type,
                                       total_tokens, total_tool_use_count)
            VALUES ('run1', ?, ?, 'Explore', 5000, 8)
            """,
            (session_id, MACHINE),
        )
    conn.commit()


def test_context_prefers_source_transcript(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import json as _json

    from wdi.llm import context

    src = tmp_path / "s1.jsonl"
    full_prompt = "FULL PROMPT " + "z" * 500
    src.write_text(
        _json.dumps({"type": "user", "message": {"role": "user", "content": full_prompt}}) + "\n",
        encoding="utf-8",
    )
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, started_at, source_path)
        VALUES ('s1', ?, 'claude', '2026-06-01T10:00:00Z', ?)
        """,
        (MACHINE, str(src)),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, machine_id, stream_id, idx, role, content_preview)
        VALUES ('s1', ?, 'main', 0, 'user', 'truncated db preview')
        """,
        (MACHINE,),
    )
    conn.commit()

    # Source present -> full prompt, not the DB preview.
    assert context.user_prompts(conn, "s1") == [full_prompt]


def test_context_falls_back_to_db_without_source(conn: sqlite3.Connection) -> None:
    from wdi.llm import context

    _session(conn)  # no source_path
    assert context.user_prompts(conn, "s1") == ["add a feature please"]


def test_context_reads_codex_source_transcript(conn: sqlite3.Connection, tmp_path: Path) -> None:
    import json as _json

    from wdi.llm import context

    src = tmp_path / "rollout.jsonl"
    full_prompt = "FULL CODEX PROMPT " + "x" * 500
    src.write_text(
        "\n".join(
            [
                _json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-01T10:00:00Z",
                        "payload": {"type": "user_message", "message": full_prompt},
                    }
                ),
                _json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-01T10:00:01Z",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, started_at, source_path)
        VALUES ('s-codex', ?, 'codex', '2026-06-01T10:00:00Z', ?)
        """,
        (MACHINE, str(src)),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, machine_id, stream_id, idx, role, content_preview)
        VALUES ('s-codex', ?, 'main', 0, 'user', 'truncated db preview')
        """,
        (MACHINE,),
    )
    conn.commit()

    assert context.user_prompts(conn, "s-codex") == [full_prompt]


def test_context_omits_observed_tool_events_from_user_prompts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    import json as _json

    from wdi.llm import context

    src = tmp_path / "observer.jsonl"
    human_prompt = "Please check why the import is slow."
    observed_read = (
        "<observed_from_primary_session>\n"
        "<what_happened>Read</what_happened>\n"
        '<parameters>{"file_path":"src/app.py"}</parameters>\n'
        "<result>RAW FILE CONTENTS</result>\n"
        "</observed_from_primary_session>"
    )
    src.write_text(
        "\n".join(
            [
                _json.dumps({"type": "user", "message": {"role": "user", "content": human_prompt}}),
                _json.dumps(
                    {"type": "user", "message": {"role": "user", "content": observed_read}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, agent, started_at, source_path)
        VALUES ('s-observer', ?, 'claude', '2026-06-01T10:00:00Z', ?)
        """,
        (MACHINE, str(src)),
    )
    conn.commit()

    assert context.user_prompts(conn, "s-observer") == [human_prompt]
    workflow = context.workflow_recommendations_context(conn, "s-observer")
    assert human_prompt in workflow
    assert "RAW FILE CONTENTS" not in workflow
    assert "<what_happened>Read</what_happened>" not in workflow


def test_summary_context_uses_compact_signals_without_raw_tool_io(
    conn: sqlite3.Connection,
) -> None:
    _session(conn)
    conn.execute(
        """
        UPDATE sessions SET context_window = 1000, input_tokens = 800, cache_read_tokens = 200,
            output_tokens = 100, reasoning_tokens = 100, time_to_first_token_ms = 250
        WHERE session_id = 's1'
        """
    )
    conn.execute(
        """
        INSERT INTO tool_calls
            (session_id, machine_id, stream_id, idx, tool_name, category, target,
             input_preview, output_preview)
        VALUES ('s1', ?, 'main', 0, 'shell_command', 'exec', 'pytest -q',
                'RAW COMMAND INPUT', 'RAW TOOL OUTPUT')
        """,
        (MACHINE,),
    )
    conn.commit()

    from wdi.llm import context

    summary = context.summary_context(conn, "s1")

    assert '"context_pressure": 1.0' in summary
    assert '"time_to_first_token_ms": 250' in summary
    assert '"reasoning_ratio": 0.1' in summary
    assert "RAW COMMAND INPUT" not in summary
    assert "RAW TOOL OUTPUT" not in summary


def test_parse_response_plain_json() -> None:
    score, data = _parse_response('{"score": 75, "summary": "s"}')
    assert score == 75.0
    assert data["summary"] == "s"


def test_parse_response_with_code_fence() -> None:
    score, data = _parse_response('```json\n{"score": 60}\n```')
    assert score == 60.0
    assert data["score"] == 60


def test_parse_response_non_json_falls_back() -> None:
    score, data = _parse_response("not json")
    assert score is None
    assert data == {"raw": "not json"}


def test_run_analyzer_stores_and_caches(conn: sqlite3.Connection) -> None:
    _session(conn)
    backend = FakeBackend()
    analyzer = ANALYZERS["prompt-quality"]

    stats = run_analyzer(conn, backend, analyzer, SessionFilter(), model="haiku", limit=10)
    assert stats.analyzed == 1
    assert len(backend.calls) == 1
    assert "add a feature please" in backend.calls[0][0]

    row = Repository(conn).get_llm_analysis("s1", "prompt_quality", analyzer.version)
    assert row is not None
    assert row["score"] == 80.0
    assert row["model"] == "haiku"

    # Cached: a second run does not call the backend again.
    stats2 = run_analyzer(conn, backend, analyzer, SessionFilter(), model="haiku", limit=10)
    assert stats2.analyzed == 0
    assert len(backend.calls) == 1

    # force re-analyzes the cached session, overwriting the stored result.
    backend.response = '{"score": 42, "summary": "redone"}'
    stats3 = run_analyzer(
        conn, backend, analyzer, SessionFilter(), model="opus", limit=10, force=True
    )
    assert stats3.analyzed == 1
    assert len(backend.calls) == 2
    redone = Repository(conn).get_llm_analysis("s1", "prompt_quality", analyzer.version)
    assert redone is not None
    assert redone["score"] == 42.0
    assert redone["model"] == "opus"


def test_cache_invalidated_by_prompt_version(conn: sqlite3.Connection) -> None:
    _session(conn)
    backend = FakeBackend()
    analyzer = ANALYZERS["prompt-quality"]
    Repository(conn).upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version="oldversion",
        model="haiku",
        score=10.0,
        result={"summary": "stale"},
    )

    stats = run_analyzer(conn, backend, analyzer, SessionFilter(), model="haiku", limit=10)

    # Different current version -> session is still considered un-analyzed.
    assert stats.analyzed == 1
    assert len(backend.calls) == 1


def test_backend_failure_is_recorded_not_raised(conn: sqlite3.Connection) -> None:
    _session(conn)
    analyzer = ANALYZERS["prompt-quality"]

    stats = run_analyzer(conn, FailingBackend(), analyzer, SessionFilter(), model="haiku", limit=10)

    assert stats.analyzed == 0
    assert stats.failed == 1
    assert stats.errors


def test_agent_efficiency_skips_sessions_without_subagents(conn: sqlite3.Connection) -> None:
    _session(conn, "s1", with_subagent=False)
    _session(conn, "s2", with_subagent=True)
    backend = FakeBackend()
    analyzer = ANALYZERS["agent-efficiency"]

    stats = run_analyzer(conn, backend, analyzer, SessionFilter(), model="sonnet", limit=10)

    assert stats.analyzed == 1
    assert stats.skipped_not_applicable == 1


def test_estimate_counts_sessions_and_prices_alias(conn: sqlite3.Connection) -> None:
    _session(conn, "s1")
    _session(conn, "s2")
    pricing = PricingResolver({"claude-haiku-4-5": ModelPricing(input=1.0, output=5.0)})
    analyzer = ANALYZERS["prompt-quality"]

    est = estimate_analyzer(
        conn, analyzer, SessionFilter(), model="haiku", limit=10, pricing=pricing
    )

    assert est.sessions == 2
    assert est.input_tokens > 0
    assert est.output_tokens == 2 * 600
    # Short alias 'haiku' is mapped to the full pricing key.
    assert est.cost is not None and est.cost > 0


def test_estimate_reports_progress_per_candidate(conn: sqlite3.Connection) -> None:
    _session(conn, "s1")
    _session(conn, "s2")
    analyzer = ANALYZERS["prompt-quality"]
    seen: list[tuple[int, int]] = []

    est = estimate_analyzer(
        conn,
        analyzer,
        SessionFilter(),
        model="haiku",
        limit=10,
        pricing=PricingResolver({}),
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert est.sessions == 2
    assert seen == [(1, 2), (2, 2)]


def test_estimate_cost_none_for_unpriced_model(conn: sqlite3.Connection) -> None:
    _session(conn, "s1")
    analyzer = ANALYZERS["prompt-quality"]

    est = estimate_analyzer(
        conn, analyzer, SessionFilter(), model="mystery", limit=10, pricing=PricingResolver({})
    )

    assert est.sessions == 1
    assert est.cost is None


def test_estimate_excludes_not_applicable_sessions(conn: sqlite3.Connection) -> None:
    _session(conn, "s1", with_subagent=False)
    _session(conn, "s2", with_subagent=True)
    analyzer = ANALYZERS["agent-efficiency"]

    est = estimate_analyzer(
        conn, analyzer, SessionFilter(), model="sonnet", limit=10, pricing=PricingResolver({})
    )

    # Only the session with subagents would be sent to the LLM.
    assert est.sessions == 1


def test_estimate_skips_already_cached(conn: sqlite3.Connection) -> None:
    _session(conn, "s1")
    analyzer = ANALYZERS["prompt-quality"]
    Repository(conn).upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version=analyzer.version,
        model="haiku",
        score=50.0,
        result={"summary": "cached"},
    )

    est = estimate_analyzer(
        conn, analyzer, SessionFilter(), model="haiku", limit=10, pricing=PricingResolver({})
    )

    assert est.sessions == 0


def test_resolve_analyzers() -> None:
    assert len(resolve_analyzers(None)) == 4
    assert resolve_analyzers("summary")[0].key == "summary"
    assert resolve_analyzers("workflow-recommendations")[0].analysis_type == (
        "workflow_recommendations"
    )
    with pytest.raises(KeyError):
        resolve_analyzers("nope")
