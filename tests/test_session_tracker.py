"""WD-141: the daemon's session tracker, findings schedule, and Context hand-off."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_daemon import _decision_request, _policy_request, enqueue, start, status, stop, wait_for

from agent_watchdog import daemon
from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.daemon import (
    FINDINGS_INTERVAL_SECONDS,
    _policy_server,
    _record_findings_failures,
    _SessionTracker,
    mutate_registry,
)
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.rules import engine
from agent_watchdog.rules.api import Decision
from agent_watchdog.state import SessionState, initial
from agent_watchdog.storage import Store

PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _finish(session_id: str, number: int, *, kind: EventKind = "tool.finish") -> Envelope:
    return Envelope(
        provider="codex",
        project_id=PROJECT,
        session_id=session_id,
        kind=kind,
        source="hook",
        received_at=START + timedelta(seconds=number),
        payload={
            "codex": {
                "tool_name": "Bash",
                "content": {
                    "tool_input": {"command": "pytest"},
                    "tool_response": {"exit_code": 1, "stdout": "boom"},
                },
            }
        },
    )


def _repeat(store: Store, session_id: str, count: int = 3) -> None:
    for number in range(count):
        store.put(_finish(session_id, number))


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "project", PROJECT) as opened:
        yield opened


def _findings(store: Store) -> set[str]:
    rows = store.connection.execute("SELECT DISTINCT session_id FROM session_findings")
    return {row[0] for row in rows}


def test_absorbed_states_are_visible_to_the_decision_channel(store):
    tracker = _SessionTracker()
    _repeat(store, "s1", 2)

    assert tracker.session("codex", "s1") is None
    tracker.absorb("p", store.touched)

    state = tracker.session("codex", "s1")
    assert state is not None and state.calls == 2
    assert tracker.session("codex", "s1", "child") is None


def test_a_restarted_tracker_restores_states_and_the_unrefreshed_backlog(store):
    _repeat(store, "s1")
    _repeat(store, "s2")
    store.refresh_session_findings("codex", "s1")

    restarted = _SessionTracker()
    restarted.load("p", store)

    state = restarted.session("codex", "s1")
    assert state is not None and state.calls == 3
    done, failures = restarted.refresh("p", store, now=0.0)
    assert (done, failures) == (1, [])  # only s2 was behind
    assert _findings(store) == {"s1", "s2"}


def test_load_drops_states_of_sessions_retention_removed(store):
    tracker = _SessionTracker()
    _repeat(store, "s1")
    _repeat(store, "s2")
    tracker.absorb("p", store.touched)
    store.purge_provider_session("codex", "s1")

    tracker.load("p", store)

    assert tracker.session("codex", "s1") is None
    assert tracker.session("codex", "s2") is not None


def test_a_session_is_refreshed_at_most_once_per_interval(store):
    tracker = _SessionTracker()
    _repeat(store, "s1")
    tracker.absorb("p", store.touched)
    assert tracker.refresh("p", store, now=100.0) == (1, [])

    store.put(_finish("s1", 10))
    tracker.absorb("p", store.touched)
    assert tracker.refresh("p", store, now=100.0 + FINDINGS_INTERVAL_SECONDS - 1) == (0, [])
    assert store.pending_sessions() == [("codex", "s1")]

    assert tracker.refresh("p", store, now=100.0 + FINDINGS_INTERVAL_SECONDS) == (1, [])
    assert store.pending_sessions() == []


def test_an_ended_session_is_refreshed_without_waiting_out_the_interval(store):
    tracker = _SessionTracker()
    _repeat(store, "s1")
    tracker.absorb("p", store.touched)
    tracker.refresh("p", store, now=100.0)
    store.put(_finish("s1", 20, kind="session.end"))
    tracker.absorb("p", store.touched)

    assert tracker.refresh("p", store, now=101.0) == (1, [])
    assert store.pending_sessions() == []


def test_one_tick_spends_its_budget_and_leaves_the_rest_for_the_next(store):
    tracker = _SessionTracker()
    for name in ("s1", "s2", "s3"):
        _repeat(store, name)
    tracker.absorb("p", store.touched)

    first = tracker.refresh("p", store, now=1.0, budget=0.0)
    second = tracker.refresh("p", store, now=1.0, budget=0.0)
    third = tracker.refresh("p", store, now=1.0, budget=0.0)

    assert [first[0], second[0], third[0]] == [1, 1, 1]
    assert _findings(store) == {"s1", "s2", "s3"}
    assert tracker.refresh("p", store, now=1.0, budget=0.0) == (0, [])


def test_a_failing_refresh_is_reported_and_retried_only_after_the_interval(store, monkeypatch):
    tracker = _SessionTracker()
    _repeat(store, "s1")
    tracker.absorb("p", store.touched)
    calls = []

    def failing(provider, session_id, **_):
        calls.append(session_id)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "refresh_session_findings", failing)

    done, failures = tracker.refresh("p", store, now=0.0)
    assert done == 0 and [type(error) for error in failures] == [sqlite3.OperationalError]
    assert tracker.refresh("p", store, now=1.0) == (0, [])
    assert calls == ["s1"]
    tracker.refresh("p", store, now=FINDINGS_INTERVAL_SECONDS)
    assert calls == ["s1", "s1"]


def test_a_failure_is_logged_once_per_episode(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    logged = []
    monkeypatch.setattr(daemon, "_log", lambda *args, **fields: logged.append(fields))
    seen: set[tuple[str, str]] = set()
    error = sqlite3.OperationalError("disk I/O error")

    for _ in range(3):
        _record_findings_failures(paths, None, PROJECT, [error], seen)
    assert [item["decision"] for item in logged] == ["unavailable"]

    _record_findings_failures(paths, None, PROJECT, [], seen)
    _record_findings_failures(paths, None, PROJECT, [error], seen)
    assert len(logged) == 2


def _capture_session(monkeypatch):
    seen = []

    # A PreToolUse/Agent call reaches exactly one built-in, so one entry per request.
    def evaluate(rule, context, *, now):
        seen.append(context.session)
        return Decision()

    monkeypatch.setattr(engine, "evaluate", evaluate)
    return seen


def test_the_calling_agents_state_reaches_the_rules_over_the_socket(paths, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    state = SessionState("claude", "session-1", "", calls=7)
    tracker = _SessionTracker()
    tracker.absorb("p", {("claude", "session-1", ""): state})
    seen = _capture_session(monkeypatch)

    with _policy_server(paths, tracker):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text(encoding="utf-8"))
        _policy_request(discovery["port"], **_decision_request(discovery, root))
        _policy_request(discovery["port"], **_decision_request(discovery, root, agent_id="child"))
        _policy_request(discovery["port"], **_decision_request(discovery, root, session_id="x"))

    assert seen == [state, None, None]


def test_a_rule_without_a_tracker_sees_no_state(paths, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    seen = _capture_session(monkeypatch)

    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text(encoding="utf-8"))
        _policy_request(discovery["port"], **_decision_request(discovery, root))

    assert seen == [None]


def test_initial_state_is_unknown_not_zero_for_what_was_never_observed():
    state = initial("claude", "s", "")
    assert state.coordinator_model is None and state.turn_id is None and state.last_activity is None


@pytest.fixture
def paths(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    yield paths
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])


def test_a_running_daemon_materializes_findings_and_survives_a_restart(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    database = paths.project_data(project.id) / "events.sqlite3"

    def read(query):
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                return db.execute(query).fetchall()
        except sqlite3.Error:
            return []

    def calls():
        return [json.loads(row[0])["calls"] for row in read("SELECT state_json FROM session_state")]

    start(paths)
    for number in range(3):
        event = _finish("s1", number).model_copy(update={"project_id": project.id})
        assert enqueue(paths, event)

    wait_for(lambda: read("SELECT rule FROM session_findings"))
    assert {row[0] for row in read("SELECT rule FROM session_findings")} >= {"identical_error"}
    assert calls() == [3]

    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    event = _finish("s1", 9).model_copy(update={"project_id": project.id})
    assert enqueue(paths, event)
    wait_for(lambda: calls() == [4])
