"""Synthetic Codex rollout tool grammars, bound to the named provider versions."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_transcripts import codex, hook, usage, write_rollout

from agent_watchdog.config import Limits
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import READER, enrich


def item(kind, call_id, **fields):
    return {
        "type": "response_item",
        "timestamp": "2026-10-15T10:00:01Z",
        "payload": {"type": kind, "call_id": call_id, **fields},
    }


def tools(store):
    return [
        event
        for event in store.events()
        if event.source == "transcript" and event.kind in ("tool.start", "tool.finish")
    ]


@pytest.mark.parametrize("version", ["0.153.4", "0.160.0", "0.162.0-alpha.17.2"])
def test_synchronous_tool_input_output_preserves_raw_records_and_provenance(tmp_path, version):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    call = item("function_call", "call-1", name="exec_command", arguments='{"cmd":"echo test"}')
    output = item(
        "function_call_output", "call-1", output="Process exited with code 0\nOutput:\ntest"
    )
    write_rollout(path, "session", [call, output, usage("session", "response", total=4, output=2)])
    path.write_text(path.read_text().replace('"0.153.4"', json.dumps(version)), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        result = enrich(store)
        observed = tools(store)
        assert result.accepted == 3
        assert not result.failures
        assert enrich(store).accepted == 0
    assert [event.kind for event in observed] == ["tool.start", "tool.finish"]
    assert all(event.native_event_id == "call-1" for event in observed)
    assert all(event.provider_version == version for event in observed)
    assert all(
        event.occurred_at == datetime(2026, 10, 15, 10, 0, 1, tzinfo=UTC) for event in observed
    )
    assert all(codex(event)["reader"] == "codex-tools-v1" for event in observed)
    assert codex(observed[0])["content"]["tool_input"] == call["payload"]["arguments"]
    assert codex(observed[1])["content"]["tool_response"] == output["payload"]["output"]
    assert codex(observed[0])["content"]["record"] == call["payload"]
    assert observed[0].availability["tool_outcome"] == "unknown"


def test_custom_patch_and_code_mode_content_blocks_stay_raw(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    patch = "*** Begin Patch\n*** Add File: sample.txt\n+test\n*** End Patch"
    nested = [{"type": "text", "text": '{"session_id": 42, "output": "pending"}'}]
    records = [
        item("custom_tool_call", "patch-1", name="apply_patch", input=patch),
        item("custom_tool_call_output", "patch-1", output="Success. Updated sample.txt"),
        item(
            "custom_tool_call", "exec-1", name="exec", input="await tools.exec_command({cmd: 'x'})"
        ),
        item("custom_tool_call_output", "exec-1", output=nested),
    ]
    write_rollout(path, "session", records)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 4
        observed = tools(store)
    assert [codex(event)["tool_name"] for event in observed if event.kind == "tool.start"] == [
        "apply_patch",
        "exec",
    ]
    assert codex(observed[0])["content"]["tool_input"] == patch
    assert codex(observed[-1])["content"]["tool_response"] == nested
    assert len(observed) == 4  # JS text cannot manufacture nested tool events.


def test_yielded_execution_and_polls_are_raw_calls_without_synthetic_completion(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    records = [item("function_call", "run", name="exec_command", arguments='{"cmd":"work"}')]
    for call_id in ("run", "poll-1", "poll-2"):
        if call_id != "run":
            records.append(
                item("function_call", call_id, name="write_stdin", arguments='{"session_id":42}')
            )
        records.append(
            item("function_call_output", call_id, output={"session_id": 42, "output": ""})
        )
    records.append(
        item("function_call", "missing", name="exec_command", arguments='{"cmd":"missing"}')
    )
    write_rollout(path, "session", records)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 7
        observed = tools(store)
    assert len([event for event in observed if event.kind == "tool.finish"]) == 3
    assert observed[-1].kind == "tool.start"
    assert all(event.availability["tool_outcome"] == "unknown" for event in observed)


def test_partial_line_restart_and_path_alias_do_not_duplicate_tools_or_usage(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    start = item("function_call", "run", name="exec_command", arguments='{"cmd":"work"}')
    finish = json.dumps(item("function_call_output", "run", output={"exit_code": 0}))
    write_rollout(path, "session", [start])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(finish[:35])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            finish[35:] + "\n" + json.dumps(usage("session", "response", total=5, output=2)) + "\n"
        )
    with Store(tmp_path / "data", project) as store:
        assert enrich(store).accepted == 2
        (tmp_path / "sub").mkdir()
        store.put(hook(project, "session", tmp_path / "sub" / ".." / path.name))
        assert enrich(store).accepted == 0
        assert len(tools(store)) == 2
        assert len([event for event in store.events() if event.kind == "usage"]) == 1


def test_native_ids_are_scoped_by_conversation_agent(tmp_path):
    project = uuid4()
    session = "11111111-1111-1111-1111-111111111111"
    child = "22222222-2222-2222-2222-222222222222"
    parent_path = tmp_path / f"rollout-{session}.jsonl"
    child_path = tmp_path / f"rollout-{child}.jsonl"
    record = item("function_call", "same-call", name="exec_command", arguments="{}")
    write_rollout(parent_path, session, [record])
    write_rollout(child_path, session, [record], thread=child)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, parent_path))
        store.put(hook(project, session, child_path))
        assert enrich(store).accepted == 2
        observed = tools(store)
    assert {event.agent_id for event in observed} == {None, child}
    assert len({event.event_id for event in observed}) == 2
    assert all(event.session_id == session for event in observed)


def test_unsupported_tool_version_keeps_usage_and_one_durable_gap(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        "session",
        [
            item("function_call", "call", name="exec_command", arguments="{}"),
            usage("session", "response", total=4, output=2),
        ],
    )
    path.write_text(path.read_text().replace('"0.153.4"', '"99.0.0"'), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 1
        assert enrich(store).accepted == 0
        assert not tools(store)
        gaps = [event for event in store.events() if event.kind == "observation.gap"]
        assert store.transcript_sources()[0]["last_error"] is None
    assert len(gaps) == 1
    assert gaps[0].availability["tool_execution"] == "unavailable"
    assert "usage" not in gaps[0].availability


def test_capture_disabled_keeps_identity_without_raw_provider_content(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        "session",
        [
            item(
                "function_call", "call", name="exec_command", arguments='{"cmd":"secret-command"}'
            ),
            item("function_call_output", "call", output="secret-output"),
        ],
    )
    with Store(tmp_path / "data", project, limits=Limits(capture_content=False)) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 2
        observed = tools(store)
    assert all(codex(event)["content"] == "omitted" for event in observed)
    assert all(event.availability["content"] == "unavailable" for event in observed)
    assert all("secret" not in event.model_dump_json() for event in observed)


def test_oversized_tool_output_is_unavailable_without_stopping_usage(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        "session",
        [
            item("function_call_output", "call", output="x" * 12000),
            usage("session", "response", total=4, output=2),
        ],
    )
    with Store(tmp_path / "data", project, limits=Limits(payload_bytes=8192)) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 2
        event = tools(store)[0]
        assert len([event for event in store.events() if event.kind == "usage"]) == 1
    assert codex(event)["content"] == "omitted"
    assert codex(event)["metadata"]["omission_reason"] == "payload_limit"
    assert event.availability["content"] == "unavailable"
    assert event.availability["tool_outcome"] == "unknown"


def test_structured_execution_records_keep_explicit_native_identity_and_exit_evidence(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    payloads = [
        {"type": "exec_command_begin", "call_id": "cmd", "command": ["echo", "test"]},
        {
            "type": "exec_command_end",
            "call_id": "cmd",
            "exit_code": 1,
            "aggregated_output": "failed",
        },
        {
            "type": "patch_apply_begin",
            "call_id": "edit",
            "changes": {"sample.txt": {"type": "add"}},
        },
        {"type": "patch_apply_end", "call_id": "edit", "success": False, "stderr": "cancelled"},
    ]
    write_rollout(
        path, "session", [{"type": "event_msg", "payload": payload} for payload in payloads]
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 4
        observed = tools(store)
    assert [event.kind for event in observed] == ["tool.start", "tool.finish"] * 2
    assert codex(observed[1])["content"]["tool_response"]["exit_code"] == 1
    assert codex(observed[3])["content"]["tool_response"]["success"] is False


def test_legacy_reader_backfills_tools_without_repeating_usage(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, "session", [usage("session", "response", total=4, output=2)])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 1
        source = store.transcript_sources()[0]
        store.connection.execute("UPDATE transcript_sources SET reader=?", (READER,))
        # Simulate a legacy reader that advanced over raw tools before this upgrade.
        text = path.read_text()
        path.write_text(
            text
            + json.dumps(item("function_call", "call", name="exec_command", arguments="{}"))
            + "\n",
            encoding="utf-8",
        )
        store.connection.execute(
            "UPDATE transcript_sources SET offset=?,size=?,mtime=?",
            (path.stat().st_size, path.stat().st_size, path.stat().st_mtime_ns),
        )
        assert source["reader"] is not None
        assert enrich(store).accepted == 1
        assert len(tools(store)) == 1
        assert len([event for event in store.events() if event.kind == "usage"]) == 1


def test_explicit_nested_command_items_keep_their_own_native_identity(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    native = {
        "type": "CommandExecution",
        "id": "exec-native",
        "command": "echo test",
        "cwd": "/synthetic",
        "process_id": "42",
    }
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_started",
                "thread_id": "session",
                "turn_id": "turn",
                "item": native | {"status": "in_progress"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session",
                "turn_id": "turn",
                "item": native
                | {
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "test",
                    "duration": {"secs": 0, "nanos": 10},
                },
            },
        },
    ]
    write_rollout(path, "session", records)
    path.write_text(path.read_text().replace('"0.153.4"', '"0.162.0-alpha.17.2"'), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 2
        observed = tools(store)
    assert [event.kind for event in observed] == ["tool.start", "tool.finish"]
    assert all(event.native_event_id == "exec-native" for event in observed)
    assert all(event.turn_id == "turn" for event in observed)
    assert all(codex(event)["metadata"]["nested_native_item"] is True for event in observed)
    assert codex(observed[0])["content"]["tool_response"]["session_id"] == "42"
    assert "session_id" not in codex(observed[1])["content"]["tool_response"]
    assert codex(observed[1])["content"]["tool_response"]["exit_code"] == 0
    assert codex(observed[1])["content"]["record"] == records[1]["payload"]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_nested_file_change_items_preserve_explicit_status_and_change_content(tmp_path, status):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    native = {
        "type": "FileChange",
        "id": "edit-native",
        "status": status,
        "changes": {"sample.txt": {"type": "add", "content": "test"}},
        "stdout": "Success. Updated the following files:\nA sample.txt"
        if status == "completed"
        else "",
        "stderr": "cancelled" if status == "cancelled" else "",
    }
    record = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": "turn",
            "thread_id": "session",
            "item": native,
        },
    }
    write_rollout(path, "session", [record])
    path.write_text(path.read_text().replace('"0.153.4"', '"0.162.0-alpha.17.2"'), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 1
        event = tools(store)[0]
    assert event.kind == "tool.finish"
    assert event.native_event_id == "edit-native"
    assert codex(event)["tool_name"] == "apply_patch"
    assert codex(event)["content"]["tool_input"] == {"changes": native["changes"]}
    assert codex(event)["content"]["tool_response"]["status"] == status
    assert codex(event)["content"]["record"] == record["payload"]
    assert codex(event)["metadata"]["native_item_type"] == "FileChange"
    assert event.availability["tool_outcome"] == "unknown"
