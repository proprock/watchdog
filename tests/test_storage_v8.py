"""Schema v8: incremental session state and materialized per-session findings."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agent_watchdog import resources
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.storage import QuotaExceeded, Store

PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _finish(session_id: str, number: int, *, provider: str = "codex", command: str = "pytest"):
    return Envelope(
        provider=provider,
        project_id=PROJECT,
        session_id=session_id,
        kind="tool.finish",
        source="hook",
        received_at=START + timedelta(seconds=number),
        payload={
            provider: {
                "tool_name": "Bash",
                "tool_use_id": f"t{number}",
                "content": {
                    "tool_input": {"command": command},
                    "tool_response": {"exit_code": 1, "stdout": "boom"},
                },
            }
        },
    )


def _repeat(store: Store, session_id: str, count: int = 3, **fields) -> None:
    for number in range(count):
        store.put(_finish(session_id, number, **fields))


def _rows(store: Store, table: str) -> list[tuple]:
    return store.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "project"


def test_a_new_store_is_schema_v8_with_the_session_tables(root):
    with Store(root, PROJECT) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 8
        tables = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master")}
        assert {"session_state", "session_findings", "session_analysis"} <= tables


def test_put_updates_the_session_state_atomically_with_the_event(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1", 2)

        state = store.touched["codex", "s1", ""]
        assert (state.calls, state.failures) == (2, 2)
        stored = store.session_states()["codex", "s1", ""]
        assert stored == state


def test_a_duplicate_event_is_not_applied_twice(root):
    event = _finish("s1", 0)
    with Store(root, PROJECT) as store:
        assert store.put(event) is True
        assert store.put(event) is False

        assert store.touched["codex", "s1", ""].calls == 1


def test_state_survives_a_restart_and_keeps_counting(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1", 2)
    with Store(root, PROJECT) as reopened:
        assert reopened.session_states()["codex", "s1", ""].calls == 2

        reopened.put(_finish("s1", 5))

        assert reopened.touched["codex", "s1", ""].calls == 3


def test_refresh_materializes_findings_and_clears_the_pending_mark(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1")
        assert store.pending_sessions() == [("codex", "s1")]

        store.refresh_session_findings("codex", "s1")

        assert store.pending_sessions() == []
        rules = {row[2] for row in _rows(store, "session_findings")}
        assert {"repeated_tool_outcome", "identical_error"} <= rules
        store.put(_finish("s1", 9))
        assert store.pending_sessions() == [("codex", "s1")]


def test_refresh_replaces_rather_than_appends(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1")
        store.refresh_session_findings("codex", "s1")
        first = _rows(store, "session_findings")

        store.refresh_session_findings("codex", "s1")

        # Same findings; only the computed_at stamp moves.
        assert [row[:-1] for row in _rows(store, "session_findings")] == [row[:-1] for row in first]


def test_a_v7_database_upgrades_and_replays_state_and_findings(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1")
        _repeat(store, "s2", 2, provider="claude")
        expected_state = store.session_states()
        store.refresh_session_findings("codex", "s1")
        expected_findings = _rows(store, "session_findings")
    with sqlite3.connect(root / "events.sqlite3") as raw:
        for table in ("session_state", "session_findings", "session_analysis"):
            raw.execute(f"DROP TABLE {table}")
        raw.execute("PRAGMA user_version=7")

    with Store(root, PROJECT) as upgraded:
        assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert upgraded.session_states() == expected_state
        replayed = [row[:-1] for row in _rows(upgraded, "session_findings")]
        assert replayed == [row[:-1] for row in expected_findings]
        assert upgraded.pending_sessions() == []


def test_the_migration_needs_free_space(root, monkeypatch):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1")
    with sqlite3.connect(root / "events.sqlite3") as raw:
        for table in ("session_state", "session_findings", "session_analysis"):
            raw.execute(f"DROP TABLE {table}")
        raw.execute("PRAGMA user_version=7")
    monkeypatch.setattr(resources, "available", lambda *args, **kwargs: 0)

    with pytest.raises(QuotaExceeded, match="v8"), Store(root, PROJECT):
        pass

    with sqlite3.connect(root / "events.sqlite3") as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 7


def test_state_holds_no_event_content(root):
    with Store(root, PROJECT) as store:
        store.put(_finish("s1", 0, command="echo hunter2"))
        store.refresh_session_findings("codex", "s1")

        for table in ("session_state", "session_findings", "session_analysis"):
            assert "hunter2" not in json.dumps(_rows(store, table))


def test_purging_a_session_drops_its_state_and_findings(root):
    with Store(root, PROJECT) as store:
        _repeat(store, "s1")
        _repeat(store, "s2")
        store.refresh_session_findings("codex", "s1")
        store.refresh_session_findings("codex", "s2")

        store.purge_provider_session("codex", "s1")

        for table in ("session_state", "session_findings", "session_analysis"):
            assert {row[1] for row in _rows(store, table)} == {"s2"}
        assert ("codex", "s1", "") not in store.touched


def test_retention_drops_state_of_sessions_with_no_events_left(root):
    limits = Limits(metrics_days=1)
    with Store(root, PROJECT, limits=limits) as store:
        _repeat(store, "s1")
        store.refresh_session_findings("codex", "s1")

        store.maintain(now=START + timedelta(days=30))

        for table in ("session_state", "session_findings", "session_analysis"):
            assert _rows(store, table) == []
