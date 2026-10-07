"""WD-141: the pure per-session state reducer."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agent_watchdog.config import Limits
from agent_watchdog.hooks import build_envelope
from agent_watchdog.registry import Resolution
from agent_watchdog.state import SessionState, apply, fold, initial, trailing_repeats

FIXTURE = Path(__file__).parent / "fixtures" / "hooks" / "claude.json"
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RESOLUTION = Resolution(uuid4(), uuid4(), Path("."))


def _at(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _envelope(
    payload: dict,
    *,
    provider: str = "claude",
    at: int = 0,
    capture_content: bool = True,
) -> dict:
    event = build_envelope(
        payload,
        RESOLUTION,
        Limits(capture_content=capture_content),
        provider,
        received_at=START + timedelta(seconds=at),
    )
    return event.model_dump(mode="json")


def _hook(name: str, **fields: object) -> dict:
    return {"session_id": "s1", "hook_event_name": name, **fields}


def _bash(command: str, *, exit_code: int = 0, **fields: object) -> dict:
    return _hook(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": command},
        tool_response={"stdout": "out", "exit_code": exit_code},
        **fields,
    )


def test_the_claude_fixture_sequence_gives_the_expected_counters():
    payloads = json.loads(FIXTURE.read_text(encoding="utf-8"))
    envelopes = [_envelope(payload, at=index) for index, payload in enumerate(payloads)]

    state = fold(envelopes)[("claude", "fixture-session", "")]

    assert (state.calls, state.failures, state.compactions) == (2, 1, 1)
    # SessionEnd closes the permission prompt the Notification opened.
    assert state.open_permission_prompt_since is None
    assert [item.outcome for item in state.last_signatures] == ["success", "failure"]


def test_a_failure_hook_counts_as_a_failure_even_without_an_exit_code():
    state = apply(
        initial("claude", "s1", ""),
        _envelope(_hook("PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"})),
    )

    assert (state.calls, state.failures, state.failures_in_turn) == (1, 1, 1)
    assert state.last_signatures[-1].outcome == "failure"


def test_a_nonzero_exit_code_counts_as_a_failure_and_a_new_turn_resets_the_turn_counters():
    state = initial("claude", "s1", "")
    state = apply(state, _envelope(_hook("UserPromptSubmit", prompt="go", prompt_id="p1")))
    state = apply(state, _envelope(_bash("pytest", exit_code=1, prompt_id="p1"), at=1))
    assert (state.failures, state.failures_in_turn, state.turn_id) == (1, 1, "p1")

    state = apply(state, _envelope(_hook("UserPromptSubmit", prompt="again", prompt_id="p2"), at=2))

    assert (state.failures, state.failures_in_turn, state.turn_id) == (1, 0, "p2")
    assert state.turn_started_at == _at(2)


def test_edits_accumulate_until_a_non_edit_call_finishes():
    state = initial("claude", "s1", "")
    for name in ("Edit", "Write"):
        state = apply(
            state,
            _envelope(_hook("PostToolUse", tool_name=name, tool_input={"file_path": "a"})),
        )
    assert state.edits_since_last_call == 2

    state = apply(state, _envelope(_bash("pytest")))

    assert state.edits_since_last_call == 0


def _edit(**fields: object) -> dict:
    return _hook("PostToolUse", tool_name="Edit", tool_input={"file_path": "a.py"}, **fields)


def test_a_test_run_after_an_edit_verifies_it_and_a_later_edit_unverifies_it():
    state = initial("claude", "s1", "")
    assert state.edited_without_verification is False

    state = apply(state, _envelope(_edit(prompt_id="p1")))
    assert (state.edits_in_turn, state.edited_without_verification) == (1, True)

    # A command that is not a test runner does not verify anything.
    state = apply(state, _envelope(_bash("git status", prompt_id="p1"), at=1))
    assert state.edited_without_verification is True

    # A failing run still ran the tests.
    state = apply(state, _envelope(_bash("uv run pytest -q", exit_code=1, prompt_id="p1"), at=2))
    assert (state.edits_in_turn, state.edited_without_verification) == (1, False)

    state = apply(state, _envelope(_edit(prompt_id="p1"), at=3))
    assert (state.edits_in_turn, state.edited_without_verification) == (2, True)


def test_a_new_turn_forgets_the_previous_turns_edits():
    state = apply(initial("claude", "s1", ""), _envelope(_edit(prompt_id="p1")))

    state = apply(state, _envelope(_hook("UserPromptSubmit", prompt="next", prompt_id="p2"), at=1))

    assert (state.edits_in_turn, state.edited_without_verification) == (0, False)


def test_verification_state_never_keeps_the_command_text():
    state = apply(initial("claude", "s1", ""), _envelope(_edit()))
    state = apply(state, _envelope(_bash("cargo test --secret-flag"), at=1))

    assert "secret-flag" not in state.to_json()


def test_an_uncaptured_shell_command_may_have_been_the_test_run():
    state = apply(initial("claude", "s1", ""), _envelope(_edit()))

    state = apply(state, _envelope(_bash("anything"), capture_content=False, at=1))

    assert state.edited_without_verification is False


def test_state_stored_before_the_verification_fields_loads_with_safe_defaults():
    stored = json.loads(initial("claude", "s1", "").to_json())
    del stored["edits_in_turn"], stored["verified_since_edit"]

    state = SessionState.from_json(json.dumps(stored))

    assert state.edited_without_verification is False


def test_trailing_repeats_counts_identical_calls_at_the_end_only():
    state = initial("claude", "s1", "")
    assert trailing_repeats(state) == 0

    for index, command in enumerate(("a", "b", "b", "b")):
        state = apply(state, _envelope(_bash(command), at=index))
    assert trailing_repeats(state) == 3

    state = apply(state, _envelope(_bash("c"), at=9))
    assert trailing_repeats(state) == 1


def test_trailing_repeats_needs_known_hashes_and_the_same_outcome():
    state = initial("claude", "s1", "")
    for index in range(3):
        state = apply(state, _envelope(_bash("same"), capture_content=False, at=index))
    assert trailing_repeats(state) == 0

    state = initial("claude", "s1", "")
    for index, code in enumerate((0, 1, 0)):
        state = apply(state, _envelope(_bash("same", exit_code=code), at=index))
    assert trailing_repeats(state) == 1


def test_usage_without_an_agent_id_sets_the_coordinator_model():
    usage = _envelope(_hook("PostToolUse", tool_name="Bash"))
    usage["kind"] = "usage"
    usage["payload"] = {"claude": {"model": "claude-opus-4-8"}}

    state = apply(initial("claude", "s1", ""), usage)

    assert state.coordinator_model == "claude-opus-4-8"


def test_a_permission_prompt_opens_and_the_next_tool_start_closes_it():
    state = initial("claude", "s1", "")
    state = apply(
        state,
        _envelope(_hook("Notification", notification_type="permission_prompt"), at=5),
    )
    assert state.open_permission_prompt_since == _at(5)

    state = apply(state, _envelope(_hook("PreToolUse", tool_name="Bash"), at=9))

    assert state.open_permission_prompt_since is None


def test_signatures_are_hashes_only_and_bounded():
    state = initial("claude", "s1", "")
    for index in range(20):
        state = apply(state, _envelope(_bash(f"secret-command-{index}"), at=index))

    assert len(state.last_signatures) == 16
    assert "secret-command" not in state.to_json()
    assert all(len(item.input_hash or "") == 64 for item in state.last_signatures)


def test_omitted_content_leaves_the_hashes_unknown():
    state = apply(initial("claude", "s1", ""), _envelope(_bash("pytest"), capture_content=False))

    signature = state.last_signatures[-1]
    assert (signature.input_hash, signature.output_hash, signature.outcome) == (
        None,
        None,
        "unknown",
    )


def test_apply_is_pure_and_the_json_round_trips():
    before = initial("claude", "s1", "")
    envelope = _envelope(_bash("pytest"))

    first = apply(before, envelope)
    second = apply(before, envelope)

    assert first == second
    assert before == initial("claude", "s1", "")
    assert SessionState.from_json(first.to_json()) == first


def test_out_of_order_events_never_move_last_activity_backwards():
    state = apply(initial("claude", "s1", ""), _envelope(_bash("a"), at=10))

    state = apply(state, _envelope(_bash("b"), at=3))

    assert state.last_activity == _at(10)
    assert state.calls == 2


def test_subagent_events_get_their_own_state_and_session_less_events_none():
    states = fold(
        [
            _envelope(_bash("a")),
            _envelope(_bash("b", agent_id="child")),
            _envelope({"hook_event_name": "PostToolUse"}),
        ]
    )

    assert sorted(states) == [("claude", "s1", ""), ("claude", "s1", "child")]


def test_session_end_marks_the_state_ended_and_later_activity_reopens_it():
    state = apply(initial("claude", "s1", ""), _envelope(_hook("SessionEnd"), at=1))
    assert state.ended_at == _at(1)

    state = apply(state, _envelope(_bash("pytest"), at=2))

    assert state.ended_at is None
