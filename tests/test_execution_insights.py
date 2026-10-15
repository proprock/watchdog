"""Execution projections shared by inspection and insight consumers (WD-164)."""

from datetime import datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
from helpers.insights import store
from test_insights_context_tokens import codex_request
from test_storage_v6 import BASE

from agent_watchdog.events import Envelope
from agent_watchdog.insights import errors, timeline, tokens
from agent_watchdog.inspection import database, finish_status, tool_finishes


class WindowOptions(TypedDict):
    provider: str | None
    session_id: str | None
    since: datetime | None
    until: datetime | None


OPTIONS: WindowOptions = {"provider": "codex", "session_id": "s1", "since": None, "until": None}


def call(kind, offset, tool, call_id, *, source="hook", agent=None, tool_input=None, response=None):
    payload = {
        "tool_name": tool,
        "content": {"tool_input": tool_input, "tool_response": response},
    }
    if source == "hook":
        payload.update(
            tool_use_id=call_id,
            hook_event_name="PreToolUse" if kind == "tool.start" else "PostToolUse",
        )
    else:
        payload.update(reader="codex-rollout-v1", call_id=call_id)
    return Envelope(
        provider="codex",
        project_id=uuid4(),
        session_id="s1",
        agent_id=agent,
        kind=kind,
        source=source,
        received_at=BASE + timedelta(seconds=offset),
        payload={"codex": payload},
    )


@pytest.mark.parametrize("tool", ["Bash", "exec_command"])
def test_command_class_accepts_explicit_cmd_input(tool):
    assert tokens.command_class(tool, {"cmd": "uv run pytest -q"}) == f"{tool}: uv run pytest"
    assert tokens.command_class(tool, {}) == f"{tool}: (unknown)"


def test_inspection_loads_start_context_before_window_and_deduplicates_sources(tmp_path):
    start = call(
        "tool.start", 1, "exec_command", "c1", source="transcript", tool_input={"cmd": "git status"}
    )
    transcript = call(
        "tool.finish",
        3,
        "exec_command",
        "c1",
        source="transcript",
        response="Process exited with code 0\nFinal output:\nclean",
    )
    hook = call("tool.finish", 4, "Bash", "c1", response={"exit_code": 0, "stdout": "clean"})
    paths, project = store(tmp_path, [start, transcript, hook])
    options: WindowOptions = {**OPTIONS, "since": BASE + timedelta(seconds=2)}

    _types, finishes = tool_finishes(paths, project, **options)
    assert len(finishes) == 1
    assert timeline.tool_name(finishes[0]) == "Bash"
    assert timeline.tool_content(finishes[0])[0] == {"command": "git status"}
    assert finish_status(finishes[0]) == "success"
    with database(paths, project) as db:
        projected = timeline.events(db, ("tool.finish",), **options)
        assert [event.event_id for event in projected] == [event.event_id for event in finishes]
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3


def test_tokens_count_paired_outputs_once_without_recounting_usage(tmp_path):
    events = [
        codex_request(0, "r1", 10_000),
        call(
            "tool.start",
            1,
            "exec_command",
            "c1",
            source="transcript",
            tool_input={"cmd": "uv run pytest"},
        ),
        call(
            "tool.finish",
            2,
            "exec_command",
            "c1",
            source="transcript",
            response="Process exited with code 0\nFinal output:\npassed",
        ),
        call(
            "tool.finish",
            3,
            "Bash",
            "c1",
            tool_input={"command": "uv run pytest"},
            response={"exit_code": 0, "stdout": "passed"},
        ),
        codex_request(4, "r2", 12_000),
    ]
    paths, project = store(tmp_path, events)

    draft = tokens.build(paths, project, **OPTIONS)
    assert draft.coverage["usage_rows"] == 2
    assert draft.coverage["tool_finishes"] == 1
    assert draft.facts["attributed_to_tool_calls"] == 2_000
    assert draft.facts["by_class"][0]["class"] == "Bash: uv run pytest"
    assert draft.facts["by_class"][0]["calls"] == 1
    assert len(draft.items) == 1


def test_call_identity_is_scoped_to_agent_and_missing_input_stays_unknown(tmp_path):
    events = [
        call(
            "tool.start", 1, "exec_command", "shared", agent="a", tool_input={"cmd": "git status"}
        ),
        call("tool.finish", 2, "exec_command", "shared", agent="b", response={"exit_code": 1}),
        call("tool.finish", 3, "exec_command", "shared", agent="a", response={"exit_code": 0}),
    ]
    paths, project = store(tmp_path, events)
    _types, finishes = tool_finishes(paths, project, **OPTIONS)
    by_agent = {event.agent_id: event for event in finishes}
    assert len(finishes) == 2
    assert timeline.tool_content(by_agent["a"])[0] == {"command": "git status"}
    assert timeline.tool_content(by_agent["b"])[0] is None
    assert finish_status(by_agent["b"]) == "failure"


def test_yielded_command_and_repeated_polls_have_one_completed_outcome(tmp_path):
    events = [
        codex_request(0, "r1", 10_000),
        call("tool.start", 1, "exec_command", "command", tool_input={"cmd": "uv run pytest"}),
        call(
            "tool.finish",
            2,
            "exec_command",
            "command",
            response={"session_id": 17, "output": "running"},
        ),
        call("tool.start", 3, "write_stdin", "poll1", tool_input={"session_id": 17, "chars": ""}),
        call(
            "tool.finish",
            4,
            "write_stdin",
            "poll1",
            response={"session_id": 17, "output": "still running"},
        ),
        call("tool.start", 5, "write_stdin", "poll2", tool_input={"session_id": 17, "chars": ""}),
        call(
            "tool.finish", 6, "write_stdin", "poll2", response={"exit_code": 1, "output": "failed"}
        ),
        codex_request(7, "r2", 12_000),
    ]
    paths, project = store(tmp_path, events)
    _types, finishes = tool_finishes(paths, project, **OPTIONS)
    assert len(finishes) == 1
    assert finish_status(finishes[0]) == "failure"
    assert timeline.tool_name(finishes[0]) == "Bash"
    assert timeline.tool_content(finishes[0])[0] == {"command": "uv run pytest"}

    token_draft = tokens.build(paths, project, **OPTIONS)
    assert token_draft.coverage["usage_rows"] == 2
    assert token_draft.coverage["tool_finishes"] == 1
    assert token_draft.facts["attributed_to_tool_calls"] == 2_000
    error_draft = errors.build(paths, project, **OPTIONS)
    assert error_draft.facts["tool_finishes"] == 1
    assert error_draft.facts["failures"] == 1
    assert error_draft.facts["by_tool"] == {"Bash": 1}
    with database(paths, project) as db:
        projected = timeline.events(db, ("tool.finish",), **OPTIONS)
    assert [event.event_id for event in projected] == [event.event_id for event in finishes]
