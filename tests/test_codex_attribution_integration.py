"""Synthetic rollouts exercise enrichment through the subagent evidence report."""

import json

from helpers.insights import store
from test_codex_attribution_reader import context
from test_insights_modes import NONE
from test_transcripts import CHILD, PARENT, hook, usage, write_rollout

from agent_watchdog.insights import subagents
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich


def tool(second, name, call, *, arguments=None, output=None):
    payload = {"call_id": call}
    if name is not None:
        payload.update(type="function_call", name=name, arguments=json.dumps(arguments))
    else:
        payload.update(type="function_call_output", output=json.dumps(output))
    return {
        "type": "response_item",
        "timestamp": f"2026-09-07T10:00:{second:02d}Z",
        "payload": payload,
    }


def test_rollout_contexts_launch_wait_and_child_usage_reach_report_without_duplication(tmp_path):
    paths, project = store(tmp_path, [])
    parent_path = tmp_path / f"rollout-{PARENT}.jsonl"
    child_path = tmp_path / f"rollout-{CHILD}.jsonl"
    write_rollout(
        parent_path,
        PARENT,
        [
            context("turn-1", "parent-model", "high"),
            tool(1, "spawn_agent", "spawn", arguments={"message": "Inspect parser"}),
            tool(2, None, "spawn", output={"agent_id": CHILD}),
            tool(5, "wait", "wait", arguments={"ids": [CHILD]}),
            tool(6, None, "wait", output={"status": {CHILD: {"completed": "Parser summary"}}}),
            context("turn-2", "future-model", "low", second=9),
        ],
    )
    response = usage(PARENT, "child-response", total=12, output=3)
    response["timestamp"] = "2026-09-07T10:00:04Z"
    write_rollout(
        child_path,
        PARENT,
        [
            context("turn-1", "child-model", "medium", second=3),
            response,
        ],
        thread=CHILD,
    )
    with Store(paths.project_data(project.id), project.id) as writer:
        writer.put(hook(project.id, PARENT, parent_path))
        writer.put(hook(project.id, PARENT, child_path).model_copy(update={"agent_id": CHILD}))
        assert enrich(writer).failures == ()
        assert enrich(writer).accepted == 0
        usage_ids = writer.usage_native_ids("codex", PARENT, CHILD)
        assert usage_ids == {"child-response"}
    (item,) = subagents.build(paths, project, **NONE).items
    assert item["agent_id"] == CHILD
    assert item["models"] == ["child-model"]
    assert item["reasoning_efforts"] == ["medium"]
    assert item["coordinator_model"] == "parent-model"
    assert item["result"] == "Parser summary"
    assert item["requests"] == 1
    assert item["input_read_tokens"] == 9
    assert item["output_tokens"] == 3


def test_coordinator_model_before_report_window_is_available_at_launch(tmp_path):
    from datetime import timedelta

    from test_insights_modes import call, request
    from test_storage_v6 import BASE

    paths, project = store(
        tmp_path,
        [
            request(0, "coordinator-request", 100, model="claude-parent"),
            call(
                2,
                tool="Agent",
                tool_input={"prompt": "Inspect parser"},
                response={"agentId": "child"},
            ),
            request(3, "child-request", 100, agent_id="child", model="claude-child"),
        ],
    )
    (item,) = subagents.build(
        paths,
        project,
        provider=None,
        session_id=None,
        since=BASE + timedelta(seconds=1),
        until=None,
    ).items
    assert item["coordinator_model"] == "claude-parent"
    assert item["request_configurations"][0]["coordinator_model"] == "claude-parent"
