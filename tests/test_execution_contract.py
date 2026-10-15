"""Reader, state and reports agree on native nested execution evidence."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_transcript_tools import item
from test_transcripts import codex, hook, usage, write_rollout

from agent_watchdog.analysis import RULE_VERSION, analyze, finding_fingerprint
from agent_watchdog.events import Envelope
from agent_watchdog.execution import VERSION, result_facts
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich


def native(kind, identifier, **fields):
    return {
        "type": "event_msg",
        "timestamp": "2026-10-15T10:00:02Z" if kind == "FileChange" else "2026-10-15T10:00:03Z",
        "payload": {
            "type": "item_completed",
            "thread_id": "session",
            "turn_id": "turn",
            "item": {"type": kind, "id": identifier, **fields},
        },
    }


def test_native_nested_command_and_edit_feed_state_and_report_once_after_restart(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    records = [
        native(
            "FileChange",
            "edit",
            status="completed",
            changes={},
            stdout="Success. Updated files",
            stderr="",
        ),
        native(
            "CommandExecution",
            "run",
            command=["python", "-m", "pytest"],
            cwd="/fixture",
            status="completed",
            exit_code=0,
            aggregated_output="1 passed",
        ),
        item(
            "custom_tool_call",
            "outer",
            name="exec",
            input="await tools.exec_command({cmd:'pytest'})",
        ),
        item(
            "custom_tool_call_output",
            "outer",
            output=[{"type": "text", "text": "Script completed\nOutput:\n"}],
        ),
        usage("session", "response", total=10, output=2),
    ]
    write_rollout(path, "session", records)
    path.write_text(path.read_text().replace('"0.153.4"', '"0.162.0-alpha.17.2"'), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 5
        state = store.session_states()["codex", "session", ""]
        assert state.calls == 3 and state.edits_in_turn == 1
        assert state.verified_since_edit
        report = analyze(store.events())
        assert report["metrics"]["tool_outcomes"] == {
            "success": 2,
            "failure": 0,
            "interrupt": 0,
            "unknown": 1,
        }
        assert report["metrics"]["usage"]["records"] == 1
    with Store(tmp_path / "data", project) as store:
        assert enrich(store).accepted == 0
        assert store.session_states()["codex", "session", ""] == state
        assert analyze(store.events())["metrics"] == report["metrics"]


@pytest.mark.parametrize("problem", ["missing_id", "wrong_thread"])
def test_incomplete_native_identity_has_an_explicit_gap_without_false_completion(tmp_path, problem):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    record = native("CommandExecution", "run", command=["pytest"], status="completed", exit_code=0)
    if problem == "missing_id":
        del record["payload"]["item"]["id"]
    else:
        record["payload"]["thread_id"] = "other-agent"
    write_rollout(path, "session", [record])
    path.write_text(path.read_text().replace('"0.153.4"', '"0.162.0-alpha.17.2"'), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, "session", path))
        assert enrich(store).accepted == 1
        assert store.session_states()["codex", "session", ""].calls == 0
        gaps = [e for e in store.events() if e.kind == "observation.gap"]
        assert len(gaps) == 1
        assert codex(gaps[0])["metadata"]["reason"] in {
            "tool_call_id_unavailable",
            "tool_thread_mismatch",
        }


def test_codex_findings_bind_verdicts_to_the_execution_projection():
    project = uuid4()
    events = [
        Envelope(
            project_id=project,
            provider="codex",
            session_id="session",
            kind="tool.finish",
            source="hook",
            received_at=datetime(2026, 10, 15, 10, 0, number, tzinfo=UTC),
            payload={
                "codex": {
                    "tool_name": "Bash",
                    "tool_use_id": str(number),
                    "content": {
                        "tool_input": {"command": "pytest"},
                        "tool_response": {"exit_code": 1, "output": "failure"},
                    },
                }
            },
        )
        for number in range(3)
    ]
    report = analyze(events)
    for finding in report["findings"]:
        assert finding["rule_version"] == RULE_VERSION
        assert finding["execution_projection"] == VERSION
        assert finding["fingerprint"] != finding_fingerprint(
            finding["rule"], RULE_VERSION, finding["evidence_ids"]
        )
        assert set(finding["execution_evidence_ids"]) <= {str(e.event_id) for e in events}


@pytest.mark.parametrize(
    "value", [[], {}, {"status": {}}, {"status": []}, {"exit_code": True}, {"exit_code": "0"}]
)
def test_malformed_result_fields_remain_unknown(value):
    assert result_facts(value).outcome == "unknown"
