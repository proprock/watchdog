"""Behavioral tests for metrics derived from imported Watchdog facts."""

import json
from pathlib import Path

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "insights.sqlite")
    Repository(connection).ensure_machine("m1")
    connection.execute(
        "INSERT INTO sessions (session_id, machine_id, agent) VALUES ('s1', 'm1', 'claude')"
    )
    yield connection
    connection.close()


def fact(conn, rowid, kind, *, machine="m1", project="p1", session="s1", **values):
    event_id = f"e{rowid}"
    at = f"2026-01-01T00:00:{rowid:02d}Z"
    provider = values.pop("provider", "claude")
    payload = values.pop("payload", {})
    envelope = json.dumps({"provider": provider, "kind": kind, "payload": {provider: payload}})
    conn.execute(
        "INSERT INTO hook_events (machine_id, project_id, event_rowid, event_id, received_at, session_id, kind, envelope) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (machine, project, rowid, event_id, at, session, kind, envelope),
    )
    fields = {
        "machine_id": machine,
        "project_id": project,
        "event_rowid": rowid,
        "event_id": event_id,
        "provider": provider,
        "session_id": session,
        "kind": kind,
        "received_at_us": rowid * 1_000_000,
        **values,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO hook_facts ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )


def test_permission_wait_closes_on_matching_resume(conn):
    from wdi.metrics.hooks import session_hook_metrics

    fact(conn, 1, "waiting", notification_type="permission_prompt", turn_id="t1", agent_id="a")
    fact(conn, 2, "tool.start", turn_id="t1", agent_id="other")
    fact(conn, 3, "tool.start", turn_id="t1", agent_id="a")
    result = session_hook_metrics(conn, "s1")
    assert result["permission_waits"] == 1
    assert result["incomplete_permission_waits"] == 0
    assert result["permission_wait_ms"] == 2000


def test_incomplete_permission_wait_is_unknown(conn):
    from wdi.metrics.hooks import session_hook_metrics

    fact(conn, 1, "waiting", notification_type="permission_prompt", turn_id="t1")
    fact(conn, 2, "turn.start", project="other", turn_id="t1")
    result = session_hook_metrics(conn, "s1")
    assert result["permission_waits"] == 1
    assert result["incomplete_permission_waits"] == 1
    assert result["permission_wait_ms"] is None


def test_permission_wait_does_not_pair_different_turns(conn):
    from wdi.metrics.hooks import session_hook_metrics

    fact(conn, 1, "waiting", notification_type="permission_prompt", turn_id="t1")
    fact(conn, 2, "tool.finish", turn_id="t2")
    result = session_hook_metrics(conn, "s1")
    assert result["incomplete_permission_waits"] == 1
    assert result["permission_wait_ms"] is None


def test_tool_duration_uses_observed_finish_values_and_preserves_unknown(conn):
    from wdi.metrics.hooks import session_hook_metrics

    fact(conn, 1, "tool.finish", tool_name="Read", tool_duration_ms=125)
    fact(conn, 2, "tool.finish", tool_name="Read", tool_duration_ms=75)
    fact(conn, 3, "tool.finish", tool_name="Write")
    assert session_hook_metrics(conn, "s1")["tool_durations_ms"] == {"Read": 200, "Write": None}


def test_only_delivered_controls_are_counted(conn):
    from wdi.metrics.hooks import session_hook_metrics

    fact(
        conn,
        1,
        "control",
        payload={"action": "deny", "rule": "guard", "delivered_at": "2026-01-01T00:00:01Z"},
    )
    fact(
        conn,
        2,
        "control",
        payload={"action": "deny", "rule": "guard", "response_sent_at": "2026-01-01T00:00:02Z"},
    )
    fact(conn, 3, "control", payload={"action": "deny", "rule": "guard", "delivered_at": None})
    assert session_hook_metrics(conn, "s1")["control_actions"] == [
        {"action": "deny", "rule": "guard", "count": 2}
    ]


def test_reconcile_tokens_scopes_threshold_and_unknown(conn):
    from wdi.metrics.hooks import reconcile_tokens

    conn.execute(
        "INSERT INTO messages (session_id, machine_id, stream_id, idx, role, input_tokens, output_tokens, cache_read_tokens) VALUES ('s1', 'm1', 'main', 0, 'assistant', 101, 51, 20)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, machine_id, stream_id, idx, role, input_tokens) VALUES ('s1', 'm1', 'agent-a', 0, 'assistant', 21)"
    )
    fact(conn, 1, "usage", input_tokens=100, output_tokens=50, cached_input_tokens=20)
    fact(conn, 2, "usage", agent_id="a", input_tokens=20)
    fact(conn, 3, "usage", machine="m2", input_tokens=999)
    rows = {(r["scope"], r["metric"]): r for r in reconcile_tokens(conn)}
    assert len(rows) == 8
    assert rows[("main", "input_tokens")] == {
        "session_id": "s1",
        "machine_id": "m1",
        "provider": "claude",
        "scope": "main",
        "metric": "input_tokens",
        "wdi": 101,
        "watchdog": 100,
        "differs": False,
    }
    assert rows[("main", "output_tokens")]["differs"] is True
    assert rows[("subagent", "input_tokens")]["differs"] is True
    assert rows[("main", "cache_write_input_tokens")]["differs"] is None
    assert rows[("main", "cache_write_input_tokens")]["watchdog"] is None


def test_reconcile_nonzero_against_zero_differs(conn):
    from wdi.metrics.hooks import reconcile_tokens

    conn.execute(
        "INSERT INTO messages (session_id, machine_id, idx, role, input_tokens) VALUES ('s1', 'm1', 0, 'assistant', 1)"
    )
    fact(conn, 1, "usage", input_tokens=0)
    row = next(
        r for r in reconcile_tokens(conn) if r["scope"] == "main" and r["metric"] == "input_tokens"
    )
    assert (row["wdi"], row["watchdog"], row["differs"]) == (1, 0, True)
