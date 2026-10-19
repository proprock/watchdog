"""Offline Codex delegation identity, chronology and missing-evidence contracts."""

import json
from datetime import timedelta
from uuid import uuid4

from helpers.insights import store
from test_insights_modes import NONE, call, event, request
from test_storage_v6 import BASE, codex_usage

from agent_watchdog.insights import subagents


def context(offset, model, effort, *, agent_id=None, received=None, session="s1"):
    value = event(
        "unknown",
        received if received is not None else offset,
        provider="codex",
        session=session,
        agent_id=agent_id,
        metadata={
            "record_type": "turn_context",
            "model": model,
            "reasoning_effort": effort,
            "attribution_projection": "wd-165.v1",
        },
    )
    return value.model_copy(
        update={"occurred_at": BASE + timedelta(seconds=offset), "source": "transcript"}
    )


def tool(
    offset,
    name,
    arguments,
    response=None,
    *,
    call_id=None,
    kind="tool.finish",
    agent_id=None,
    session="s1",
    received=None,
):
    value = event(
        kind,
        received if received is not None else offset,
        provider="codex",
        session=session,
        agent_id=agent_id,
        tool_name=name,
        tool_use_id=call_id or f"tool-{offset}",
        content={"tool_input": arguments, "tool_response": response},
    )
    return value.model_copy(update={"occurred_at": BASE + timedelta(seconds=offset)})


def usage(offset, agent, *, session="s1", received=None):
    value = codex_usage(
        uuid4(),
        session=session,
        offset=offset,
        response_id=f"r{offset}",
        turn_id=None,
        delta={"input_tokens": 100, "output_tokens": 10},
    )
    return value.model_copy(
        update={
            "agent_id": agent,
            "received_at": BASE + timedelta(seconds=received if received is not None else offset),
        }
    )


def report(tmp_path, events):
    paths, project = store(tmp_path, events)
    return subagents.build(paths, project, **NONE)


def test_codex_launch_and_operations_join_by_explicit_child_identity(tmp_path):
    draft = report(
        tmp_path,
        [
            context(0, "gpt-parent", "high"),
            tool(
                1,
                "functions.spawn_agent",
                json.dumps({"message": "Inspect parser", "agent_type": "explorer"}),
                call_id="launch",
                kind="tool.start",
            ),
            tool(2, None, None, json.dumps({"agent_id": "child"}), call_id="launch"),
            context(3, "gpt-child", "medium", agent_id="child"),
            usage(4, "child"),
            tool(
                5,
                "functions.send_input",
                {"id": "child", "message": "Check callers"},
                {"submission_id": "m1"},
            ),
            tool(
                6,
                "functions.wait",
                {"ids": ["child"]},
                {"status": {"child": {"completed": "Parser report"}}},
            ),
            tool(7, "functions.close_agent", {"id": "child"}, {"status": "closed"}),
        ],
    )
    (item,) = draft.items
    assert item["agent_id"] == "child"
    assert item["task"] == "Inspect parser"
    assert item["agent_type"] == "explorer"
    assert item["coordinator_model"] == "gpt-parent"
    assert item["coordinator_reasoning_effort"] == "high"
    assert item["models"] == ["gpt-child"]
    assert item["reasoning_efforts"] == ["medium"]
    assert item["result"] == "Parser report"
    assert [op["operation"] for op in item["operations"]] == ["message", "wait", "close"]
    assert item["observed"]["launch_result"] is True
    assert item["observed"]["usage"] is True
    assert item["task_success"] is None
    assert item["unpriced_tokens"] == 110


def test_requests_overrides_and_inherited_config_are_distinct_and_temporal(tmp_path):
    draft = report(
        tmp_path,
        [
            context(0, "gpt-before", "low", received=100),
            tool(
                2,
                "collaboration.spawn_agent",
                {
                    "task_name": "parser",
                    "message": "Map parser",
                    "model": "gpt-requested",
                    "reasoning_effort": "high",
                },
                {"agent_id": "child"},
                received=101,
            ),
            context(5, "gpt-after", "medium", received=102),
            context(3, "gpt-observed", "low", agent_id="child", received=103),
            usage(4, "child", received=104),
            context(6, "gpt-observed-2", "high", agent_id="child", received=105),
            usage(7, "child", received=106),
        ],
    )
    (item,) = draft.items
    assert item["requested_model"] == "gpt-requested"
    assert item["requested_reasoning_effort"] == "high"
    assert item["coordinator_model"] == "gpt-before"
    assert item["inherited_configuration"]["model"] == "gpt-before"
    assert item["models"] == ["gpt-observed", "gpt-observed-2"]
    assert item["reasoning_efforts"] == ["high", "low"]
    assert [row["coordinator_model"] for row in item["request_configurations"]] == [
        "gpt-before",
        "gpt-after",
    ]
    assert all(row["evidence_id"] for row in item["request_configurations"])


def test_launch_only_missing_launch_result_and_transcript_only_stay_unknown(tmp_path):
    draft = report(
        tmp_path,
        [
            tool(
                1,
                "spawn_agent",
                {"message": "No transcript", "model": "requested"},
                {"agent_id": "only-launch"},
            ),
            tool(2, "spawn_agent", {"message": "No result"}, kind="tool.start"),
            context(3, "gpt-transcript", "low", agent_id="only-transcript"),
            usage(4, "only-transcript"),
        ],
    )
    assert len(draft.items) == 3
    by_agent = {item["agent_id"]: item for item in draft.items}
    assert by_agent["only-launch"]["models"] == []
    assert by_agent["only-launch"]["input_read_tokens"] is None
    assert by_agent["only-launch"]["requests"] is None
    assert by_agent["only-launch"]["result_bytes"] is None
    assert by_agent[None]["observed"]["launch_result"] is False
    assert by_agent["only-transcript"]["launch_evidence_id"] is None
    assert draft.coverage["agents_without_usage"] == 2
    assert draft.coverage["launches_without_identity"] == 1


def test_launch_identity_never_crosses_provider_session_or_coordinator(tmp_path):
    events = [
        call(1, tool="Agent", tool_input={"prompt": "Claude task"}, response={"agentId": "same"}),
        request(2, "claude-request", 200, agent_id="same"),
        tool(3, "spawn_agent", {"message": "Codex task"}, {"agent_id": "same"}),
        usage(4, "same"),
        tool(5, "spawn_agent", {"message": "Other session"}, {"agent_id": "same"}, session="s2"),
        usage(6, "same", session="s2"),
        tool(
            7, "spawn_agent", {"message": "Nested child"}, {"agent_id": "nested"}, agent_id="same"
        ),
        tool(8, "send_input", {"id": "nested"}, "wrong coordinator"),
        tool(9, "send_input", {"id": "nested"}, "right coordinator", agent_id="same"),
    ]
    draft = report(tmp_path, events)
    assert len(draft.items) == 4
    tasks = {item["task"]: item for item in draft.items}
    assert tasks["Claude task"]["provider"] == "claude"
    assert tasks["Other session"]["session_id"] == "s2"
    assert len(tasks["Nested child"]["operations"]) == 1
    assert tasks["Nested child"]["coordinator_agent_id"] == "same"


def test_opaque_exec_text_and_unsupported_tool_names_do_not_create_launches(tmp_path):
    draft = report(
        tmp_path,
        [
            tool(
                1,
                "functions.exec",
                'await tools.spawn_agent({message:"fake"})',
                {"agent_id": "fake"},
            ),
            tool(2, "untrusted.spawn_agent", {"message": "fake"}, {"agent_id": "fake"}),
            tool(3, "spawn_agent", "opaque code", {"agent_id": "real"}),
        ],
    )
    (item,) = draft.items
    assert item["agent_id"] == "real"
    assert item["task"] is None


def test_future_context_outside_window_does_not_change_observed_models(tmp_path):
    paths, project = store(
        tmp_path,
        [
            context(0, "parent", "high"),
            tool(1, "spawn_agent", {"message": "Map parser"}, {"agent_id": "child"}),
            context(2, "current-child", "low", agent_id="child"),
            usage(3, "child"),
            context(30, "future-child", "high", agent_id="child"),
        ],
    )
    (item,) = subagents.build(
        paths,
        project,
        provider=None,
        session_id=None,
        since=None,
        until=BASE + timedelta(seconds=10),
    ).items
    assert item["models"] == ["current-child"]
    assert item["reasoning_efforts"] == ["low"]


def test_transcript_only_coordinator_is_unknown_without_explicit_link(tmp_path):
    (item,) = report(
        tmp_path,
        [
            context(0, "root-model", "high"),
            context(1, "child-model", "low", agent_id="child"),
            usage(2, "child"),
        ],
    ).items
    assert item["coordinator_model"] is None
    assert item["coordinator_attribution"] == "unknown"
    assert item["request_configurations"][0]["coordinator_model"] is None


def test_transcript_only_result_can_join_unique_explicit_wait_operator(tmp_path):
    (item,) = report(
        tmp_path,
        [
            context(0, "root-model", "high"),
            context(1, "child-model", "low", agent_id="child"),
            usage(2, "child"),
            tool(
                3,
                "wait",
                {"ids": ["child"]},
                {"status": {"child": {"completed": "Result without launch"}}},
            ),
        ],
    ).items
    assert item["launch_evidence_id"] is None
    assert item["coordinator_attribution"] == "operation_target"
    assert item["coordinator_model"] == "root-model"
    assert item["result"] == "Result without launch"


def test_conflicting_contexts_and_missing_fields_remain_unknown(tmp_path):
    draft = report(
        tmp_path,
        [
            context(0, "old-model", "low"),
            context(1, "first-model", "high"),
            context(1, "other-model", "high"),
            tool(2, "spawn_agent", {}, {"agent_id": "first-child"}),
            context(3, None, None),
            tool(4, "spawn_agent", {}, {"agent_id": "second-child"}),
        ],
    )
    items = {item["agent_id"]: item for item in draft.items}
    assert items["first-child"]["coordinator_model"] is None
    assert items["first-child"]["coordinator_reasoning_effort"] == "high"
    assert items["second-child"]["coordinator_model"] is None
    assert items["second-child"]["coordinator_reasoning_effort"] is None


def test_operation_without_result_is_observed_with_unknown_result(tmp_path):
    (item,) = report(
        tmp_path,
        [
            tool(1, "spawn_agent", {}, {"agent_id": "child"}),
            tool(2, "send_input", {"id": "child"}, kind="tool.start"),
        ],
    ).items
    assert item["operations"][0]["observed_result"] is False
    assert item["operations"][0]["result"] is None


def test_missing_native_launch_and_response_times_keep_comparisons_unknown(tmp_path):
    launch = tool(1, "spawn_agent", {}, {"agent_id": "child"}).model_copy(
        update={"source": "transcript", "occurred_at": None}
    )
    response = usage(3, "child").model_copy(update={"occurred_at": None})
    (item,) = report(
        tmp_path,
        [
            context(0, "root-model", "high"),
            launch,
            context(2, "child-model", "medium", agent_id="child"),
            response,
        ],
    ).items
    assert item["coordinator_model"] is None
    assert item["request_configurations"][0]["coordinator_model"] is None
    assert item["request_configurations"][0]["response_time_attribution"] == "unavailable"
