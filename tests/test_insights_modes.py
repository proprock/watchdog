"""Offline contracts for `insights workflow`, `subagents`, and `permissions` (WD-125)."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from helpers.insights import store
from test_storage_v6 import BASE, claude_usage

from agent_watchdog import insights
from agent_watchdog.events import Envelope
from agent_watchdog.insights import contract, llm, permissions, subagents, workflow

NONE = {"provider": None, "session_id": None, "since": None, "until": None}


def event(kind, offset, *, session="s1", agent_id=None, provider="claude", **fields):
    return Envelope(
        provider=provider,
        project_id=uuid4(),
        session_id=session,
        agent_id=agent_id,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={provider: fields},
    )


def call(offset, command=None, *, tool="Bash", session="s1", agent_id=None, **extra):
    tool_input = {"command": command} if command is not None else extra.pop("tool_input", {})
    return event(
        "tool.finish",
        offset,
        session=session,
        agent_id=agent_id,
        hook_event_name=extra.pop("hook_event_name", "PostToolUse"),
        tool_name=tool,
        tool_use_id=extra.pop("tool_use_id", None),
        content={"tool_input": tool_input, "tool_response": extra.pop("response", "ok")},
        metadata=extra.pop("metadata", {}),
    )


def request(offset, request_id, occupancy, *, agent_id=None, model="claude-sonnet-5"):
    usage = claude_usage(
        uuid4(),
        session="s1",
        offset=offset,
        request_id=request_id,
        model=model,
        response={
            "input_tokens": 2,
            "cache_read_input_tokens": occupancy - 2,
            "cache_creation_input_tokens": 0,
            "output_tokens": 50,
        },
    )
    return usage.model_copy(update={"agent_id": agent_id})


# workflow


def _checks(session, start):
    commands = ("uv run ruff check .", "uv run pytest -q", "git commit -m x")
    return [call(start + index, command, session=session) for index, command in enumerate(commands)]


def test_workflow_finds_a_chain_repeated_across_sessions(tmp_path):
    events = [
        *_checks("s1", 0),
        *_checks("s1", 10),
        call(15, "uv run pytest -q", session="s1"),  # back-to-back calls collapse to one step
        *_checks("s2", 20),
    ]
    paths, project = store(tmp_path, events)
    draft = workflow.build(paths, project, **NONE)
    (item,) = [item for item in draft.items if item["kind"] == "sequence"]
    assert item["steps"] == ["Bash: uv run ruff", "Bash: uv run pytest", "Bash: git commit"]
    assert (item["occurrences"], item["sessions"]) == (3, 2)
    assert item["examples"][0][0]["input"] == '{"command": "uv run ruff check ."}'


def test_workflow_ignores_plain_reading_and_editing(tmp_path):
    events = [
        call(offset, tool=tool, session=session, tool_input={"file_path": f"f{offset}"})
        for session in ("s1", "s2")
        for offset, tool in enumerate(("Read", "Edit", "Read", "Edit", "Read", "Edit"))
    ]
    paths, project = store(tmp_path, events)
    assert workflow.build(paths, project, **NONE).items == []


def test_workflow_reports_a_polling_loop_but_not_varied_work(tmp_path):
    events = [call(offset, "gh run view 42 --json status") for offset in range(4)]
    events += [call(10 + offset, f"git show HEAD~{offset}") for offset in range(4)]
    paths, project = store(tmp_path, events)
    draft = workflow.build(paths, project, **NONE)
    (loop,) = draft.items
    assert loop["kind"] == "loop"
    assert (loop["class"], loop["calls"], loop["distinct_inputs"]) == ("Bash: gh run view", 4, 1)
    assert loop["span_seconds"] == 3
    assert draft.facts["loops_listed"] == 1


# subagents


def _spawn(tmp_path, *, subagent_model="claude-sonnet-5"):
    events = [
        request(0, "c1", 20_000),
        call(
            1,
            tool="Agent",
            tool_input={"description": "Map the parser", "prompt": "Find every parser entry"},
            response={"agentId": "ag1", "isAsync": False},
        ),
        event("agent.start", 2, agent_id="ag1", metadata={"agent_type": "Explore"}),
        request(3, "a1", 8_000, agent_id="ag1", model=subagent_model),
        request(4, "a2", 12_000, agent_id="ag1", model=subagent_model),
        call(5, "rg parser", agent_id="ag1", hook_event_name="PostToolUseFailure"),
        event(
            "agent.end",
            32,
            agent_id="ag1",
            metadata={"agent_type": "Explore"},
            content={"last_assistant_message": "Parser lives in src/p.py"},
        ),
        event("agent.end", 40, agent_id="helper", metadata={"agent_type": ""}),
    ]
    return store(tmp_path, events)


def test_subagents_profile_cost_model_and_result(tmp_path):
    paths, project = _spawn(tmp_path)
    draft = subagents.build(paths, project, **NONE)
    (item,) = draft.items
    assert item["item_id"] == "A1"
    assert item["agent_type"] == "Explore"
    assert item["task"] == "Find every parser entry"
    assert item["description"] == "Map the parser"
    assert item["launch_evidence_id"] is not None
    assert (item["coordinator_model"], item["models"]) == ("claude-sonnet-5", ["claude-sonnet-5"])
    assert item["same_family_as_coordinator"] is True
    assert (item["requests"], item["input_read_tokens"], item["peak_tokens"]) == (2, 20_000, 12_000)
    assert (item["tool_calls"], item["tool_failures"]) == (1, 1)
    assert item["duration_s"] == 30
    assert item["result_bytes"] == len("Parser lives in src/p.py")
    assert draft.coverage["untyped_stops_excluded"] == 1
    by_type = draft.facts["by_type"]["claude: Explore"]
    assert (by_type["agents"], by_type["same_family_as_coordinator"]) == (1, 1)


def test_subagents_compare_model_families_not_ids(tmp_path):
    paths, project = _spawn(tmp_path, subagent_model="claude-haiku-4-5-20251001")
    (item,) = subagents.build(paths, project, **NONE).items
    assert item["same_family_as_coordinator"] is False


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", "opus"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("gpt-6-sol", "gpt-6-sol"),
        (None, None),
    ],
)
def test_model_family(model, expected):
    assert subagents.family(model) == expected


# permissions


def _prompt(offset, tool, **fields):
    message = f"Claude needs your permission to use {tool}"
    metadata = {"notification_type": "permission_prompt", "message": message} | fields
    return event("waiting", offset, hook_event_name="Notification", metadata=metadata)


def _start(offset, tool, tool_input, tool_use_id, mode="default"):
    return event(
        "tool.start",
        offset,
        hook_event_name="PreToolUse",
        tool_name=tool,
        tool_use_id=tool_use_id,
        content={"tool_input": tool_input},
        metadata={"permission_mode": mode},
    )


def test_permissions_group_prompts_with_wait_and_outcome(tmp_path):
    events = [
        _start(0, "Bash", {"command": "uv run pytest -q"}, "t1"),
        _prompt(1, "Bash"),
        call(31, "uv run pytest -q", tool_use_id="t1"),
        _start(40, "Bash", {"command": "uv run pytest tests/a.py"}, "t2"),
        _prompt(41, "Bash"),  # never finished: denied, interrupted, or lost
        _start(50, "AskUserQuestion", {"questions": []}, "t3", mode="plan"),
        _prompt(51, "AskUserQuestion"),
        call(60, tool="AskUserQuestion", tool_use_id="t3", tool_input={"questions": []}),
        _prompt(70, "WebFetch"),  # no matching call observed
        _start(80, "Read", {"file_path": "a.py"}, "t4"),
    ]
    paths, project = store(tmp_path, events)
    draft = permissions.build(paths, project, **NONE)
    first, second = draft.items
    assert (first["class"], first["prompts"]) == ("Bash: uv run pytest", 2)
    assert first["waited_s"] == {"median": 30, "max": 30, "total": 30, "unknown": 1}
    assert first["outcomes"] == {"finished": 1, "no_finish_observed": 1}
    assert first["permission_modes"] == {"default": 2}
    assert first["user_interaction"] is False
    assert first["examples"][0]["input"] == '{"command": "uv run pytest -q"}'
    assert second["tool"] == "AskUserQuestion"
    assert second["user_interaction"] is True
    assert draft.coverage["prompts_without_matched_call"] == 1
    assert draft.facts["permission_prompts"] == 4
    assert draft.facts["tool_starts_by_mode"]["claude"] == {"default": 3, "plan": 1}
    assert draft.facts["prompts_per_100_tool_starts"]["claude"] == 100.0


# shared contract


@pytest.mark.parametrize("module", [workflow, subagents, permissions])
def test_mode_schemas_are_self_contained(module):
    schema = contract.json_schema(module.Output)
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["recommendations"]["items"]["additionalProperties"] is False


def test_a_permissions_answer_is_validated_and_grounded(tmp_path):
    events = [
        _start(0, "Bash", {"command": "uv run pytest -q"}, "t1"),
        _prompt(1, "Bash"),
        call(5, "uv run pytest -q", tool_use_id="t1"),
    ]
    paths, project = store(tmp_path, events)
    answer = {
        "summary": "One gated test run.",
        "recommendations": [
            {
                "title": "Allow the quiet test run",
                "item_ids": ["P1"],
                "kind": "allow_rule",
                "rule": "Bash(uv run pytest:*)",
                "risk": "read_only",
                "target": "claude",
                "recommendation": "Allow it in project settings.",
                "draft": '{"permissions": {"allow": ["Bash(uv run pytest:*)"]}}',
                "confidence": "high",
                "evidence_ids": ["P1"],
            }
        ],
        "rule_candidates": [],
    }
    result = insights.run(
        paths,
        project,
        alias="repo",
        mode="permissions",
        provider=None,
        session_id=None,
        since=BASE - timedelta(days=1),
        until=None,
        model="sonnet",
        effort=None,
        timeout=60.0,
        max_bundle_tokens=None,
        language="English",
        dry_run=False,
        output=None,
        runner=lambda request: llm.Result(answer, None, {}),
    )
    assert result["status"] == "ok"
    assert result["recommendations"][0]["ungrounded"] is False
    broken = answer | {"recommendations": [answer["recommendations"][0] | {"risk": "safe"}]}
    rejected = insights.run(
        paths,
        project,
        alias="repo",
        mode="permissions",
        provider=None,
        session_id=None,
        since=BASE - timedelta(days=1),
        until=None,
        model="sonnet",
        effort=None,
        timeout=60.0,
        max_bundle_tokens=None,
        language="English",
        dry_run=False,
        output=None,
        runner=lambda request: llm.Result(broken, None, {}),
    )
    assert rejected["reason"] == "malformed_output"
