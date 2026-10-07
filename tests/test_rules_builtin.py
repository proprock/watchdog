"""WD-142: the shipped rules, replayed on hook fixtures and pinned case by case."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest

from agent_watchdog.config import Config, Limits, Rules, UserPaths
from agent_watchdog.hooks import EVENTS, build_envelope
from agent_watchdog.registry import Resolution
from agent_watchdog.rules.api import Context, Decision
from agent_watchdog.rules.declarative import HookEvent, evaluate
from agent_watchdog.rules.registry import BUILTIN_NAMES, RuleRegistry
from agent_watchdog.state import SessionState, apply, initial, key_of

FIXTURE = Path(__file__).parent / "fixtures" / "hooks" / "claude.json"
START = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RESOLUTION = Resolution(uuid4(), uuid4(), Path("."))
PATHS = UserPaths(Path("c"), Path("d"), Path("r"))
# The default-off rule is part of what ships, so every replay switches it on.
CONFIG = Config(rules=Rules(enabled=["stop_without_verification"]))


def builtins() -> dict:
    entries = RuleRegistry(PATHS).active(CONFIG)
    assert {entry.name for entry in entries} == set(BUILTIN_NAMES)
    return {entry.name: entry.rule for entry in entries}


def decisions(payload: dict, state: SessionState | None) -> list[Decision]:
    """What every shipped rule says about one hook call, given the state before it."""
    context = Context(PATHS, CONFIG, "claude", payload, state)
    answers = (evaluate(rule, context, now=START) for rule in builtins().values())
    return [answer for answer in answers if answer.action != "allow"]


def envelope(payload: dict, index: int) -> dict:
    event = build_envelope(
        payload, RESOLUTION, Limits(), "claude", received_at=START + timedelta(seconds=index)
    )
    return event.model_dump(mode="json")


def replay(payloads: list[dict]) -> list[list[Decision]]:
    """Feed hook payloads in order; each is judged on the state before it, as the daemon does."""
    states: dict = {}
    answers = []
    for index, payload in enumerate(payloads):
        folded = envelope(payload, index)
        key = key_of(folded)
        answers.append(decisions(payload, states.get(key) if key else None))
        if key is not None:
            states[key] = apply(states.get(key) or initial(*key), folded)
    return answers


def hook(name: str, **fields: Any) -> dict:
    return {"session_id": "s1", "cwd": "/p", "hook_event_name": name, **fields}


def bash_done(command: str, *, prompt_id: str = "p1", exit_code: int = 0) -> dict:
    return hook(
        "PostToolUse",
        tool_name="Bash",
        prompt_id=prompt_id,
        tool_input={"command": command},
        tool_response={"stdout": "same output", "exit_code": exit_code},
    )


def edit_done(prompt_id: str = "p1") -> dict:
    return hook(
        "PostToolUse",
        tool_name="Edit",
        prompt_id=prompt_id,
        tool_input={"file_path": "a.py"},
        tool_response={"ok": True},
    )


def prompt(prompt_id: str) -> dict:
    return hook("UserPromptSubmit", prompt="go", prompt_id=prompt_id)


def test_the_four_shipped_rules_are_exactly_the_builtins():
    assert sorted(builtins()) == sorted(BUILTIN_NAMES)
    assert len(BUILTIN_NAMES) == 4


def test_no_shipped_rule_fires_on_the_benign_fixture_sequence():
    payloads = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert replay(payloads) == [[]] * len(payloads)


def test_the_same_call_three_times_in_a_row_gets_one_context_nudge():
    payloads = [prompt("p1"), *[bash_done("uv run pytest -x", exit_code=1)] * 4]

    answers = replay(payloads)

    # Each call is judged on the state before it, so the fourth is the first to see
    # three identical earlier calls; a daemon that lags a call behind would need five.
    assert [len(item) for item in answers] == [0, 0, 0, 0, 1]
    (nudge,) = answers[-1]
    assert (nudge.action, nudge.rule) == ("context", "repeat_same_input_same_output")
    assert nudge.context is not None and "3 times" in nudge.context


def test_a_changed_call_resets_the_repeat_count():
    payloads = [prompt("p1"), bash_done("a"), bash_done("a"), bash_done("b"), bash_done("b")]

    assert all(not item for item in replay(payloads))


def test_editing_then_stopping_without_a_test_run_is_blocked_once_enabled():
    stop = hook("Stop", stop_hook_active=False)

    blocked = replay([prompt("p1"), edit_done(), bash_done("git status"), stop])[-1]
    verified = replay([prompt("p1"), edit_done(), bash_done("uv run pytest"), stop])[-1]
    next_turn = replay([prompt("p1"), edit_done(), prompt("p2"), stop])[-1]
    nothing_edited = replay([prompt("p1"), bash_done("git status"), stop])[-1]

    assert [(item.action, item.rule) for item in blocked] == [
        ("block", "stop_without_verification")
    ]
    assert verified == [] and next_turn == [] and nothing_edited == []


def test_a_test_run_before_the_edit_does_not_count_as_verification():
    stop = hook("Stop", stop_hook_active=False)

    answers = replay([prompt("p1"), bash_done("cargo test"), edit_done(), stop])[-1]

    assert [item.action for item in answers] == ["block"]


def coordinator_state(model: str | None) -> SessionState:
    return SessionState("claude", "s1", "", coordinator_model=model)


def agent_call(model: str | None) -> dict:
    tool_input = {"prompt": "p", "subagent_type": "general-purpose"}
    if model is not None:
        tool_input["model"] = model
    return hook("PreToolUse", tool_name="Agent", tool_input=tool_input)


@pytest.mark.parametrize(
    ("coordinator", "candidate", "lowered_to"),
    [
        ("claude-fable-5-1", "fable", "opus"),
        ("claude-opus-5-5", "opus", "sonnet"),
        ("claude-sonnet-5-5", "sonnet", "haiku"),
        ("claude-haiku-5-5", "haiku", None),
        ("claude-opus-5-5", "sonnet", None),
        ("claude-opus-5-5", None, None),
        (None, "opus", None),
    ],
)
def test_a_same_tier_subagent_is_lowered_one_tier(coordinator, candidate, lowered_to):
    answers = decisions(agent_call(candidate), coordinator_state(coordinator))

    if lowered_to is None:
        assert answers == []
        return
    (answer,) = answers
    assert (answer.action, answer.rule) == ("rewrite", "subagent_same_model")
    assert answer.updated_input is not None and answer.updated_input["model"] == lowered_to


DESTRUCTIVE = [
    "rm -rf build",
    "rm -fr build",
    "rm -r build",
    "rm -R build",
    "rm --recursive build",
    "rm -rf",
    "sudo rm -rf /",
    "cd build && rm -rf out",
    "make clean; rm -rf dist",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git reset HEAD~1 --hard",
    "git push --force",
    "git push -f origin main",
    "git push origin main --force",
    "git push --force-with-lease origin main",
    "git push origin main -f",
    "git checkout -- .",
    "git checkout main -- .",
    "git checkout -- . && ls",
    "git clean -fd",
    "git clean -f",
    "git clean -xdf",
    "git clean -d -f",
    "rm .env",
    "rm -f .env.local",
    "rm config/.env.production",
    "del .env",
    # Known false positive, pinned on purpose: the match is textual, and asking
    # about a quoted command costs one confirmation.
    'echo "rm -rf build"',
]
HARMLESS = [
    "git push",
    "git push origin main",
    "git push --set-upstream origin feature-f",
    "git push origin main && echo -f",
    "rm a.txt",
    "rm -f a.txt",
    "rm build/a.o build/b.o",
    "git rm -r --cached x",
    "git rm a.txt",
    "git checkout main",
    "git checkout -- src/a.py",
    "git checkout -- ./a.py",
    "git reset HEAD file.py",
    "git reset --soft HEAD~1",
    "git clean -n",
    "git clean --dry-run",
    "rm .env.example",
    "rm .env.sample",
    "rm .environment",
    "ls -r",
    "grep -rf patterns.txt .",
    "npm run format",
    "git log --format=%H",
    "uv run pytest -x",
]


def ask_about(command: str) -> list[Decision]:
    call = hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
    return decisions(call, None)


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_commands_ask(command):
    (answer,) = ask_about(command)

    assert (answer.action, answer.rule) == ("ask", "destructive_command")


@pytest.mark.parametrize("command", HARMLESS)
def test_ordinary_commands_pass_untouched(command):
    assert ask_about(command) == []


def test_other_tools_are_not_matched_by_the_command_rule():
    call = hook("PreToolUse", tool_name="Edit", tool_input={"file_path": "rm -rf"})

    assert decisions(call, None) == []


def test_every_event_a_rule_may_name_is_one_the_installed_hooks_deliver():
    """The installer registers every Claude event unconditionally, with no matcher; a
    rule on an event outside that list would be approved and never fire."""
    assert set(get_args(HookEvent)) <= set(EVENTS["claude"])


def test_every_shipped_rule_names_an_event_the_installed_hooks_deliver():
    for rule in builtins().values():
        assert rule.event in EVENTS["claude"]
