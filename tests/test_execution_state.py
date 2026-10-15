"""WD-164: execution observations and replay share one causal session state."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import pytest

from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.state import fold
from agent_watchdog.storage import Store

PROJECT = UUID("11111111-2222-3333-4444-555555555555")
START = datetime(2026, 10, 1, 12, tzinfo=UTC)
KEY = ("codex", "session", "")


def event(
    index: int,
    tool: str = "exec_command",
    *,
    call: str | None = None,
    tool_input: object = None,
    response: object = None,
    kind: EventKind = "tool.finish",
    agent: str | None = None,
    turn: str | None = None,
    source: Literal["hook", "transcript"] = "transcript",
) -> Envelope:
    payload = {"tool_name": tool, "content": {}}
    if call is not None:
        payload["tool_use_id"] = call
    if source == "transcript":
        payload["reader"] = "codex-rollout-v1"
    if tool_input is not None:
        payload["content"]["tool_input"] = tool_input
    if response is not None:
        payload["content"]["tool_response"] = response
    return Envelope(
        provider="codex",
        project_id=PROJECT,
        session_id="session",
        agent_id=agent,
        turn_id=turn,
        kind=kind,
        source=source,
        received_at=START + timedelta(seconds=index),
        payload={"codex": payload},
    )


def terminal(code: int = 0) -> str:
    return f"Process exited with code {code}\nFinal output:\nresult"


def replay(events: list[Envelope]):
    return fold(item.model_dump(mode="json") for item in events)[KEY]


def edit(index: int, **kwargs) -> Envelope:
    return event(
        index, "apply_patch", call=f"edit-{index}", response="Success. Updated a.py", **kwargs
    )


def test_yield_and_poll_complete_the_original_command_once(tmp_path):
    events = [
        edit(0),
        event(1, call="run", tool_input={"cmd": "uv run pytest"}, kind="tool.start"),
        event(2, call="run", response="Process running with session ID 42"),
        event(3, "write_stdin", call="poll-1", tool_input={"session_id": 42}, kind="tool.start"),
        event(4, "write_stdin", call="poll-1", response="Process running with session ID 42"),
        event(5, "write_stdin", call="poll-2", tool_input={"session_id": 42}, kind="tool.start"),
        event(6, "write_stdin", call="poll-2", response=terminal(1)),
    ]
    with Store(tmp_path, PROJECT) as store:
        for item in events[:-1]:
            store.put(item)
        assert store.session_states()[KEY].edited_without_verification
        store.put(events[-1])
        state = store.session_states()[KEY]
        assert (state.calls, state.failures, state.edits_in_turn) == (2, 1, 1)
        assert state.verified_since_edit
        assert [signature.tool for signature in state.last_signatures] == ["Edit", "Bash"]
        assert state == replay(events)
    with Store(tmp_path, PROJECT) as reopened:
        assert reopened.session_states()[KEY] == state


@pytest.mark.parametrize(
    "response", ["Process running with session ID 42", "interrupted by user", None]
)
@pytest.mark.parametrize("tool", ["exec_command", "Bash"])
def test_unfinished_or_unknown_test_commands_never_verify(response, tool):
    state = replay(
        [
            edit(0),
            event(
                1,
                tool,
                call="run",
                tool_input={"command" if tool == "Bash" else "cmd": "uv run pytest"},
                response=response,
            ),
        ]
    )
    assert state.edited_without_verification
    assert state.failures == 0


def test_completed_command_without_captured_input_cannot_verify():
    state = replay([edit(0), event(1, call="run", response=terminal())])
    assert state.edited_without_verification


def test_missing_finish_input_uses_the_matching_start():
    state = replay(
        [
            edit(0),
            event(1, call="run", tool_input={"cmd": "cargo test"}, kind="tool.start"),
            event(2, call="run", response=terminal()),
        ]
    )
    assert (state.calls, state.failures) == (2, 0)
    assert state.verified_since_edit


def test_interleaved_agent_calls_cannot_supply_each_others_input(tmp_path):
    events = [
        edit(0),
        event(1, call="shared", tool_input={"cmd": "pytest"}, kind="tool.start", agent="child"),
        event(2, call="shared", response=terminal()),
    ]
    states = fold(item.model_dump(mode="json") for item in events)
    assert states[KEY].edited_without_verification
    assert states[("codex", "session", "child")].calls == 0
    with Store(tmp_path, PROJECT) as store:
        for item in events:
            store.put(item)
        assert store.session_states() == states


def test_late_rollout_enrichment_does_not_verify_a_newer_edit_or_reset_turn(tmp_path):
    events = [
        edit(0, turn="first"),
        event(1, call="run", tool_input={"cmd": "pytest"}, source="hook", turn="first"),
        edit(2, turn="first"),
        event(3, kind="turn.start", turn="next"),
        edit(4, turn="next"),
        event(5, call="run", tool_input={"cmd": "pytest"}, response=terminal(1), turn="first"),
        event(6, kind="tool.start", call="run", tool_input={"cmd": "pytest"}, turn="first"),
    ]
    with Store(tmp_path, PROJECT) as store:
        for item in events:
            store.put(item)
        state = store.session_states()[KEY]
        assert (state.calls, state.failures, state.turn_id, state.failures_in_turn) == (
            4,
            1,
            "next",
            0,
        )
        assert state.edits_in_turn == 1 and state.edited_without_verification
        assert state == replay(events)
        assert (
            json.loads(
                store.connection.execute(
                    "SELECT envelope FROM events WHERE event_id=?", (str(events[1].event_id),)
                ).fetchone()[0]
            )["payload"]["codex"].get("execution")
            is None
        )


def test_reopening_rebuilds_only_stale_codex_execution_state_once(tmp_path):
    events = [edit(0), event(1, call="run", tool_input={"cmd": "pytest"}, response=terminal())]
    with Store(tmp_path, PROJECT) as store:
        for item in events:
            store.put(item)
        claude = Envelope(
            provider="claude",
            project_id=PROJECT,
            session_id="other",
            kind="tool.finish",
            source="hook",
            payload={"claude": {"tool_name": "Bash"}},
        )
        store.put(claude)
        claude_before = store.connection.execute(
            "SELECT state_json, updated_at FROM session_state WHERE provider='claude'"
        ).fetchone()
        expected = store.session_states()[KEY]
        stale = json.loads(expected.to_json())
        stale["calls"] = 900
        store.connection.execute(
            "UPDATE session_state SET state_json=? WHERE provider='codex'", (json.dumps(stale),)
        )
        store.connection.execute("DELETE FROM state_projection_versions WHERE provider='codex'")
    with Store(tmp_path, PROJECT) as reopened:
        assert reopened.session_states()[KEY] == expected
        assert (
            reopened.connection.execute(
                "SELECT state_json, updated_at FROM session_state WHERE provider='claude'"
            ).fetchone()
            == claude_before
        )
        assert reopened.connection.execute("PRAGMA user_version").fetchone()[0] == 8
        stamp = reopened.connection.execute(
            "SELECT updated_at FROM session_state WHERE provider='codex'"
        ).fetchone()[0]
    with Store(tmp_path, PROJECT) as reopened:
        assert (
            reopened.connection.execute(
                "SELECT updated_at FROM session_state WHERE provider='codex'"
            ).fetchone()[0]
            == stamp
        )
