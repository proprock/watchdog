"""Tests for the dashboard-level coach service."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.db.connection import apply_migrations, connect
from wdi.db.repository import Repository
from wdi.llm import coach
from wdi.llm.backend import LLMError
from wdi.metrics.compute import build_report
from wdi.metrics.filters import SessionFilter
from wdi.metrics.pricing import PricingResolver

MACHINE = "m1"

COACH_RESPONSE = json.dumps(
    {
        "summary": "Cache use is healthy; shell errors dominate waste.",
        "advice": ["Prefer Bash over PowerShell on Windows."],
        "focus_areas": ["shell reliability"],
    }
)


class FakeBackend:
    def __init__(self, response: str = COACH_RESPONSE) -> None:
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
    apply_migrations(connection)
    repo = Repository(connection)
    repo.ensure_machine(MACHINE)
    connection.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, agent, model, started_at, input_tokens,
             output_tokens, cache_read_tokens, tool_call_count)
        VALUES ('s1', ?, 'claude', 'claude-opus-4-8', '2026-06-01T10:00:00Z',
                1000, 500, 200, 3)
        """,
        (MACHINE,),
    )
    connection.commit()
    yield connection
    connection.close()


def _report(conn: sqlite3.Connection):  # noqa: ANN202
    return build_report(conn, SessionFilter(), PricingResolver({}))


def test_filter_hash_is_stable_and_filter_sensitive() -> None:
    a = coach.filter_hash(SessionFilter(agent="claude"))
    b = coach.filter_hash(SessionFilter(agent="claude"))
    c = coach.filter_hash(SessionFilter(agent="codex"))
    d = coach.filter_hash(SessionFilter(agent="claude", include_noise=True))

    assert a == b
    assert a != c
    assert a != d


def test_build_coach_context_is_compact_json(conn: sqlite3.Connection) -> None:
    context = coach.build_coach_context(_report(conn))

    data = json.loads(context)
    assert data["overview"]["sessions"] == 1
    assert "waste" in data
    assert "cache_misses" in data
    assert len(context) < 8_000


def test_run_coach_calls_backend_and_caches(conn: sqlite3.Connection) -> None:
    backend = FakeBackend()
    flt = SessionFilter()

    first = coach.run_coach(conn, backend, flt, _report(conn), model="haiku")
    second = coach.run_coach(conn, backend, flt, _report(conn), model="haiku")

    assert first.cached is False
    assert first.result["summary"].startswith("Cache use")
    assert second.cached is True
    assert len(backend.calls) == 1
    prompt, model = backend.calls[0]
    assert model == "haiku"
    assert '"sessions": 1' in prompt


def test_run_coach_force_reruns(conn: sqlite3.Connection) -> None:
    backend = FakeBackend()
    flt = SessionFilter()

    coach.run_coach(conn, backend, flt, _report(conn), model="haiku")
    result = coach.run_coach(conn, backend, flt, _report(conn), model="haiku", force=True)

    assert result.cached is False
    assert len(backend.calls) == 2


def test_run_coach_propagates_llm_error_without_caching(conn: sqlite3.Connection) -> None:
    flt = SessionFilter()

    with pytest.raises(LLMError):
        coach.run_coach(conn, FailingBackend(), flt, _report(conn), model="haiku")

    row = Repository(conn).get_coach_report(coach.filter_hash(flt), coach.coach_prompt_version())
    assert row is None


def test_run_coach_non_json_response_stored_as_raw(conn: sqlite3.Connection) -> None:
    backend = FakeBackend(response="not json at all")

    result = coach.run_coach(conn, backend, SessionFilter(), _report(conn), model="haiku")

    assert result.result == {"raw": "not json at all"}


def test_coach_cache_separates_providers_and_models(conn: sqlite3.Connection) -> None:
    backend = FakeBackend()
    flt = SessionFilter()
    for provider, model in [("claude", "haiku"), ("codex", "haiku"), ("codex", "other")]:
        result = coach.run_coach(
            conn, backend, flt, _report(conn), model=model, backend_name=provider
        )
        assert not result.cached
    assert len(backend.calls) == 3
    assert coach.run_coach(
        conn, backend, flt, _report(conn), model="other", backend_name="codex"
    ).cached


def test_coach_marks_changed_metrics_stale_and_recomputes(conn: sqlite3.Connection) -> None:
    backend = FakeBackend()
    flt = SessionFilter()
    report = _report(conn)
    coach.run_coach(conn, backend, flt, report, model="haiku")
    report.overview.estimated_cost = 42.0
    cached = coach.get_cached_coach(conn, flt, report, model="haiku", backend_name="claude")
    assert cached is not None and cached.stale
    assert not coach.run_coach(conn, backend, flt, report, model="haiku").cached
    assert len(backend.calls) == 2
