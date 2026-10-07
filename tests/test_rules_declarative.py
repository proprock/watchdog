"""WD-142: declarative rule parsing and evaluation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from agent_watchdog.config import Config, UserPaths
from agent_watchdog.rules.api import ALLOW, Context
from agent_watchdog.rules.declarative import RuleError, evaluate, parse_rule
from agent_watchdog.state import SessionState, Signature

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PATHS = UserPaths(Path("c"), Path("d"), Path("r"))


def toml(**overrides: object) -> str:
    document: dict = {
        "schema_version": 1,
        "name": "sample",
        "version": "1",
        "origin": "user",
        "provider": ["claude"],
        "event": "PreToolUse",
        "match": {"tool_name": "Bash"},
        "state": {},
        "action": {"kind": "ask", "message": "Watchdog: check this."},
    }
    document |= overrides
    return tomli_w.dumps(document)


def context(
    hook_input: dict, session: SessionState | None = None, provider: str = "claude"
) -> Context:
    return Context(PATHS, Config(), provider, hook_input, session)


def session(**fields: Any) -> SessionState:
    return SessionState("claude", "s1", "", **fields)


def bash(command: str, event: str = "PreToolUse", **fields: object) -> dict:
    return {
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        **fields,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": {"no_such_predicate": True}},
        {"extra_key": 1},
        {"match": {"tool_name": "Bash", "surprise": "x"}},
        {"action": {"kind": "explode", "message": "m"}},
        {"event": "NotAHook"},
        {"provider": []},
        {"name": "Not A Name"},
        {"match": {"input_regex": "("}},
        {"match": {"input_regex": "a" * 513}},
        {"match": {"input_regex": "(a+)+"}},
        {"match": {"input_regex": "(.*)*"}},
        {"match": {"error_regex": "(x*)*"}},
        {"state": {"failures_in_turn": {}}},
        {"state": {"failures_in_turn": {"min": 3, "max": 1}}},
        {"action": {"kind": "ask", "message": "m", "rewrite": {"tool_input.model": "haiku"}}},
        {"action": {"kind": "rewrite", "message": "m"}},
        {"action": {"kind": "rewrite", "message": "m", "rewrite": {"model": "haiku"}}},
        {"action": {"kind": "ask", "message": "{secret}"}},
        {"action": {"kind": "ask", "message": "{0}"}},
        {"action": {"kind": "ask", "message": ""}},
        {"action": {"kind": "ask", "message": "m", "expires_at": "tomorrow"}},
        # An action the adapter cannot deliver on that event would be recorded
        # as sent while the model never saw it.
        {"event": "Stop", "action": {"kind": "context", "message": "m"}},
        {"event": "PostToolUse", "action": {"kind": "deny", "message": "m"}},
        {"event": "PostToolUseFailure", "action": {"kind": "block", "message": "m"}},
    ],
)
def test_invalid_rule_bodies_are_rejected(overrides):
    with pytest.raises(RuleError):
        parse_rule(toml(**overrides))


def test_garbage_is_a_rule_error_not_a_crash():
    for text in ("", "not = [toml", "\x00\x01", "schema_version = 2"):
        with pytest.raises(RuleError):
            parse_rule(text)


def test_a_valid_rule_parses_with_defaults():
    rule = parse_rule(toml())

    assert (rule.name, rule.event, rule.priority, rule.default_enabled) == (
        "sample",
        "PreToolUse",
        100,
        True,
    )
    assert rule.action.cooldown_seconds == 0 and rule.action.max_per_turn is None


def test_a_rule_that_does_not_match_the_call_allows():
    rule = parse_rule(toml())

    assert evaluate(rule, context(bash("ls")), now=NOW).action == "ask"
    assert evaluate(rule, context(bash("ls", event="PostToolUse")), now=NOW) == ALLOW
    assert evaluate(rule, context({**bash("ls"), "tool_name": "Edit"}), now=NOW) == ALLOW
    assert evaluate(rule, context(bash("ls"), provider="codex"), now=NOW) == ALLOW
    assert evaluate(rule, context({"tool_name": "Bash"}), now=NOW) == ALLOW


def test_the_decision_names_the_rule_version_and_message():
    rule = parse_rule(toml(version="7"))

    decision = evaluate(rule, context(bash("ls")), now=NOW)

    assert (decision.action, decision.rule, decision.rule_version) == ("ask", "sample", "7")
    assert decision.reason == "Watchdog: check this."


@pytest.mark.parametrize(
    ("kind", "event", "field"),
    [
        ("deny", "PreToolUse", "reason"),
        ("ask", "PreToolUse", "reason"),
        ("log", "PreToolUse", "reason"),
        ("block", "Stop", "reason"),
        ("context", "PostToolUse", "context"),
    ],
)
def test_the_message_lands_in_the_field_the_action_uses(kind, event, field):
    rule = parse_rule(toml(event=event, action={"kind": kind, "message": "hello"}))

    decision = evaluate(rule, context(bash("ls", event=event)), now=NOW)

    assert decision.action == kind
    assert getattr(decision, field) == "hello"


def test_input_regex_reads_the_command_or_the_serialized_input():
    command_rule = parse_rule(toml(match={"input_regex": r"rm\s+-rf"}))
    assert evaluate(command_rule, context(bash("rm -rf build")), now=NOW).action == "ask"
    assert evaluate(command_rule, context(bash("echo rm")), now=NOW) == ALLOW

    json_rule = parse_rule(toml(match={"tool_name": "Edit", "input_regex": r"\.env"}))
    edit = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": ".env"},
    }
    assert evaluate(json_rule, context(edit), now=NOW).action == "ask"


def test_an_oversized_input_is_truncated_not_scanned_whole():
    rule = parse_rule(toml(match={"input_regex": "NEEDLE"}))

    late = bash("x" * (64 * 1024 + 10) + "NEEDLE")

    assert evaluate(rule, context(late), now=NOW) == ALLOW


def test_error_regex_matches_the_failure_text():
    rule = parse_rule(
        toml(
            event="PostToolUseFailure",
            match={"error_regex": "permission denied"},
            action={"kind": "log", "message": "m"},
        )
    )
    failing = {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "error": "Permission denied",
    }
    # Case matters: the pattern is applied as written.
    assert evaluate(rule, context(failing), now=NOW) == ALLOW
    failing["error"] = "x: permission denied"
    assert evaluate(rule, context(failing), now=NOW).action == "log"
    assert evaluate(rule, context({**failing, "error": 3}), now=NOW) == ALLOW


def signature(tool="Bash", input_hash="i", output_hash="o", outcome="success"):
    return Signature(tool, input_hash, output_hash, outcome, "2026-10-07T12:00:00Z")


@pytest.mark.parametrize(
    ("predicate", "state", "fires"),
    [
        ({"repeats_same_input_output": {"min": 3}}, {"last_signatures": (signature(),) * 3}, True),
        ({"repeats_same_input_output": {"min": 3}}, {"last_signatures": (signature(),) * 2}, False),
        ({"edits_since_last_call": {"max": 0}}, {"edits_since_last_call": 0}, True),
        ({"edits_since_last_call": {"max": 0}}, {"edits_since_last_call": 2}, False),
        ({"failures_in_turn": {"min": 2}}, {"failures_in_turn": 2}, True),
        ({"failures_in_turn": {"min": 2}}, {"failures_in_turn": 1}, False),
        ({"compactions_in_turn": {"min": 1, "max": 1}}, {"compactions_in_turn": 1}, True),
        ({"compactions_in_turn": {"min": 1, "max": 1}}, {"compactions_in_turn": 2}, False),
        ({"edited_without_verification": True}, {"edits_in_turn": 1}, True),
        (
            {"edited_without_verification": True},
            {"edits_in_turn": 1, "verified_since_edit": True},
            False,
        ),
        ({"edited_without_verification": False}, {"edits_in_turn": 0}, True),
        (
            {"turn_minutes": {"min": 10}},
            {"turn_started_at": (NOW - timedelta(minutes=11)).isoformat()},
            True,
        ),
        (
            {"turn_minutes": {"min": 10}},
            {"turn_started_at": (NOW - timedelta(minutes=9)).isoformat()},
            False,
        ),
    ],
)
def test_each_state_predicate(predicate, state, fires):
    rule = parse_rule(toml(state=predicate))

    decision = evaluate(rule, context(bash("ls"), session(**state)), now=NOW)

    assert (decision.action != "allow") is fires


def test_unknown_state_is_never_a_match():
    rule = parse_rule(toml(state={"failures_in_turn": {"max": 5}}))
    assert evaluate(rule, context(bash("ls"), None), now=NOW) == ALLOW

    unknown_turn = parse_rule(toml(state={"turn_minutes": {"max": 5}}))
    assert evaluate(unknown_turn, context(bash("ls"), session()), now=NOW) == ALLOW

    # A rule with no state predicate does not need the state at all.
    assert evaluate(parse_rule(toml()), context(bash("ls"), None), now=NOW).action == "ask"


def test_an_unparsable_turn_start_is_unknown():
    rule = parse_rule(toml(state={"turn_minutes": {"min": 0}}))

    assert evaluate(rule, context(bash("ls"), session(turn_started_at="garbage")), now=NOW) == ALLOW


def test_an_expired_rule_allows():
    expiring = parse_rule(
        toml(action={"kind": "ask", "message": "m", "expires_at": "2026-10-07T11:59:00Z"})
    )
    live = parse_rule(
        toml(action={"kind": "ask", "message": "m", "expires_at": "2026-10-07T12:01:00Z"})
    )

    assert evaluate(expiring, context(bash("ls")), now=NOW) == ALLOW
    assert evaluate(live, context(bash("ls")), now=NOW).action == "ask"


def test_message_placeholders_come_from_the_state():
    rule = parse_rule(
        toml(
            action={
                "kind": "context",
                "message": "Same call {repeats} times; model {coordinator_model}.",
            },
            state={"repeats_same_input_output": {"min": 2}},
        )
    )
    state = session(last_signatures=(signature(),) * 4, coordinator_model="claude-opus-5-5")

    decision = evaluate(rule, context(bash("ls"), state), now=NOW)

    assert decision.context == "Same call 4 times; model claude-opus-5-5."


def agent_call(model: str | None, **extra: object) -> dict:
    tool_input: dict = {
        "prompt": "do it",
        "description": "d",
        "subagent_type": "general-purpose",
        **extra,
    }
    if model is not None:
        tool_input["model"] = model
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input}


def downgrade_rule():
    return parse_rule(
        toml(
            match={"tool_name": "Agent"},
            state={"subagent_same_tier_as_coordinator": True},
            action={
                "kind": "rewrite",
                "message": "Watchdog: model lowered.",
                "rewrite": {"tool_input.model": "$lower_tier"},
            },
        )
    )


def test_a_same_tier_subagent_is_rewritten_one_tier_down_keeping_the_rest_of_the_input():
    state = session(coordinator_model="claude-opus-5-5")
    call = agent_call("opus", run_in_background=True)

    decision = evaluate(downgrade_rule(), context(call, state), now=NOW)

    assert decision.action == "rewrite"
    assert decision.updated_input == {
        "prompt": "do it",
        "description": "d",
        "subagent_type": "general-purpose",
        "run_in_background": True,
        "model": "sonnet",
    }
    # The hook input itself is never mutated.
    assert call["tool_input"]["model"] == "opus"


@pytest.mark.parametrize(
    ("candidate", "coordinator", "fires"),
    [
        ("sonnet", "claude-sonnet-5-5", True),
        ("haiku", "claude-haiku-5-5", False),  # already at the lowest tier
        ("haiku", "claude-opus-5-5", False),  # a different family
        ("sonnet", "claude-opus-5-5", False),
        ("opus", None, False),  # coordinator model not observed yet
        ("custom", "claude-custom-1", False),  # not a ranked tier
        (None, "claude-opus-5-5", False),  # no explicit model on the call
    ],
)
def test_same_tier_predicate_cases(candidate, coordinator, fires):
    decision = evaluate(
        downgrade_rule(),
        context(agent_call(candidate), session(coordinator_model=coordinator)),
        now=NOW,
    )

    assert (decision.action == "rewrite") is fires


def test_tiers_come_from_the_configuration():
    config = Config.model_validate({"rules": {"tiers": ["small", "large"]}})
    state = session(coordinator_model="claude-large-1")

    decision = evaluate(
        downgrade_rule(),
        Context(PATHS, config, "claude", agent_call("large"), state),
        now=NOW,
    )

    assert decision.updated_input is not None
    assert decision.updated_input["model"] == "small"
