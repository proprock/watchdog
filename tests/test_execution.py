"""Codex execution facts are derived without rewriting provider evidence."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.execution import normalize_events, result_facts


def event(kind, tool=None, call=None, data=None, response=None, *, agent=None, source="hook", at=0):
    content = {}
    if data is not None:
        content["tool_input"] = data
    if response is not None:
        content["tool_response"] = response
    return Envelope(
        provider="codex",
        project_id=UUID(int=1),
        session_id="session",
        agent_id=agent,
        kind=kind,
        source=source,
        received_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=at),
        payload={"codex": {"tool_name": tool, "tool_use_id": call, "content": content}},
    )


def finishes(events):
    return [e for e in normalize_events(events) if e.kind == "tool.finish"]


def facts(e):
    return e.payload["codex"]["execution"]


@pytest.mark.parametrize(
    ("response", "phase", "outcome"),
    [
        ({"exit_code": 0}, "completed", "success"),
        ({"exit_code": 2}, "completed", "failure"),
        ({"session_id": 42, "exit_code": None}, "running", "unknown"),
        ({"interrupted": True}, "interrupted", "interrupt"),
        ({"isError": True}, "completed", "failure"),
        (None, "unknown", "unknown"),
        ("test passed", "unknown", "unknown"),
        ("Process exited with code 1\nfixture failure", "completed", "failure"),
        (
            "Chunk ID: abc\nWall time: 1 seconds\nProcess running with session ID 42\nOutput:\n",
            "running",
            "unknown",
        ),
        (
            "Chunk ID: abc\nWall time: 1 seconds\nProcess exited with code 0\n"
            "Final output:\nProcess exited with code 3",
            "completed",
            "success",
        ),
        ("arbitrary output\nProcess exited with code 0", "unknown", "unknown"),
    ],
)
def test_only_provider_result_evidence_supplies_an_outcome(response, phase, outcome):
    actual = result_facts(response)
    assert (actual.phase, actual.outcome) == (phase, outcome)
    assert actual.provenance


def test_hook_and_rollout_have_identical_command_facts_without_mutating_raw_events():
    hook = event("tool.finish", "Bash", "c", {"command": "uv run pytest"}, {"exit_code": 1})
    start = event("tool.start", "exec_command", "c", '{"cmd":"uv run pytest"}', source="transcript")
    end = event("tool.finish", call="c", response={"exit_code": 1}, source="transcript", at=1)
    original = start.model_dump()
    one, two = finishes([hook])[0], finishes([start, end])[0]
    assert one.payload["codex"]["tool_name"] == two.payload["codex"]["tool_name"] == "Bash"
    assert one.payload["codex"]["content"] == two.payload["codex"]["content"]
    assert facts(one)["outcome"] == facts(two)["outcome"] == "failure"
    assert start.model_dump() == original


def test_yield_and_repeated_polls_count_one_completed_original_command():
    rows = [
        event("tool.start", "exec_command", "run", {"cmd": "uv run pytest"}),
        event("tool.finish", "exec_command", "run", response={"session_id": 42}, at=1),
        event("tool.finish", "write_stdin", "poll1", {"session_id": 42}, {"session_id": 42}, at=2),
        event("tool.finish", "write_stdin", "poll2", {"session_id": 42}, {"session_id": 42}, at=3),
        event("tool.finish", "write_stdin", "poll3", {"session_id": 42}, {"exit_code": 0}, at=4),
    ]
    actual = finishes(rows)
    assert len(actual) == 1
    assert actual[0].received_at == rows[-1].received_at
    assert actual[0].payload["codex"]["content"]["tool_input"] == {"command": "uv run pytest"}
    assert facts(actual[0])["outcome"] == "success"
    assert {str(row.event_id) for row in rows} <= set(facts(actual[0])["evidence_ids"])
    assert normalize_events(normalize_events(rows)) == normalize_events(rows)


def test_process_ids_do_not_cross_agent_boundaries():
    rows = [
        event("tool.finish", "exec_command", "a", {"cmd": "pytest"}, {"session_id": 42}, agent="a"),
        event(
            "tool.finish", "exec_command", "b", {"cmd": "cargo test"}, {"session_id": 42}, agent="b"
        ),
        event(
            "tool.finish", "write_stdin", "p", {"session_id": 42}, {"exit_code": 2}, agent="b", at=2
        ),
    ]
    actual = finishes(rows)
    assert len(actual) == 1
    assert actual[0].agent_id == "b"
    assert actual[0].payload["codex"]["content"]["tool_input"]["command"] == "cargo test"


def test_late_duplicate_enrichment_keeps_hook_anchor_and_adds_evidence():
    hook = event("tool.finish", "Bash", "c", {"command": "pytest"}, at=1)
    start = event("tool.start", "exec_command", "c", {"cmd": "pytest"}, source="transcript", at=10)
    output = event("tool.finish", call="c", response={"exit_code": 0}, source="transcript", at=11)
    actual = finishes([hook, start, output, output])
    assert len(actual) == 1
    assert actual[0].event_id == hook.event_id
    assert actual[0].received_at == hook.received_at
    assert facts(actual[0])["outcome"] == "success"
    assert set(facts(actual[0])["evidence_ids"]) == {str(e.event_id) for e in (hook, start, output)}


def test_missing_identity_never_guesses_that_equal_commands_are_duplicates():
    rows = [
        event("tool.finish", "Bash", data={"command": "pytest"}, response={"exit_code": 0})
        for _ in range(2)
    ]
    assert len(finishes(rows)) == 2


def test_patch_edits_and_cancellation_share_execution_provenance():
    edit = event(
        "tool.finish",
        "functions.apply_patch",
        "edit",
        "*** Begin Patch",
        "Success. Updated the following files:\nM file.py",
    )
    cancel = event("tool.finish", "exec_command", "run", {"cmd": "pytest"}, {"cancelled": True})
    actual = finishes([edit, cancel])
    assert actual[0].payload["codex"]["tool_name"] == "Edit"
    assert facts(actual[0])["outcome"] == "success"
    assert facts(actual[1])["outcome"] == "interrupt"


def test_code_mode_completion_does_not_claim_nested_commands_succeeded():
    outer = event(
        "tool.finish",
        "exec",
        "outer",
        "await tools.exec_command({cmd:'pytest'})",
        [
            {"type": "text", "text": "Script completed\nWall time 1 seconds\nOutput:\n"},
            {"type": "text", "text": '{"exit_code":0,"output":"done"}'},
        ],
    )
    actual = finishes([outer])[0]
    assert actual.payload["codex"]["tool_name"] == "exec"
    assert facts(actual)["outcome"] == "unknown"
    assert facts(actual)["phase"] == "completed"
    assert facts(actual)["command"] is None


def test_conflicting_terminal_evidence_is_unknown():
    a = event("tool.finish", "Bash", "c", {"command": "pytest"}, {"exit_code": 0})
    b = event(
        "tool.finish", "exec_command", "c", response={"exit_code": 2}, source="transcript", at=2
    )
    result = finishes([a, b])
    assert len(result) == 1
    assert facts(result[0])["outcome"] == "unknown"
    assert facts(result[0])["provenance"] == "conflicting_results"


def test_claude_events_pass_through_unchanged():
    raw = event("tool.finish", "Bash", "c", response={"exit_code": 0}).model_copy(
        update={"provider": "claude", "payload": {"claude": {"tool_name": "Bash"}}}
    )
    assert normalize_events([raw]) == [raw]


def test_projected_conflict_precedes_raw_failure_hook_fallback():
    from agent_watchdog.analysis import tool_outcome

    raw = {
        "hook_event_name": "PostToolUseFailure",
        "execution": {
            "version": "wd-164.v1",
            "outcome": "unknown",
            "provenance": "conflicting_results",
        },
    }
    assert tool_outcome(raw, "codex") == "unknown"
    assert tool_outcome(raw, "claude") == "failure"
