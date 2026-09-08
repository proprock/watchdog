import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_watchdog.analysis import (
    REPORT_SCHEMA_VERSION,
    analyze,
    diff_oscillations,
    git_diff_fingerprint,
)
from agent_watchdog.events import Envelope


def tool(project, moment, *, code, output, command="pytest tests/test_sample.py"):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id="session-1",
        kind="tool.finish",
        source="hook",
        received_at=moment,
        payload={
            "codex": {
                "tool_name": "exec",
                "content": {
                    "tool_input": {"command": command},
                    "tool_response": {"exit_code": code, "output": output},
                },
            }
        },
    )


def test_report_has_reproducible_shadow_findings_without_a_stall_verdict():
    project = uuid4()
    start = datetime(2026, 9, 7, tzinfo=UTC)
    failure = "FAILED tests/test_sample.py::test_rejects_bad_input - AssertionError\n1 failed"
    events = [
        Envelope(
            provider="codex",
            project_id=project,
            session_id="session-1",
            kind="turn.start",
            source="hook",
            received_at=start,
        ),
        *(
            tool(project, start + timedelta(seconds=number), code=1, output=failure)
            for number in (1, 2, 3)
        ),
        # A later successful process does not erase the observed three failures.
        tool(project, start + timedelta(seconds=4), code=0, output="1 passed"),
        Envelope(
            provider="codex",
            project_id=project,
            session_id="session-1",
            kind="waiting",
            source="hook",
            received_at=start + timedelta(seconds=5),
        ),
        Envelope(
            provider="codex",
            project_id=project,
            session_id="session-1",
            kind="compaction.start",
            source="hook",
            received_at=start + timedelta(seconds=6),
        ),
    ]

    report = analyze(events)

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["rule_version"] == "wd-010.v1"
    assert report["timeline"]["wall_seconds"] == 6.0
    assert report["metrics"]["compactions"] == {"started": 1, "completed": 0}
    assert report["metrics"]["tool_outcomes"] == {"failure": 3, "success": 1, "unknown": 0}
    assert report["gaps"] == ["active_time_unknown", "task_outcome_unknown", "usage_incomplete"]
    assert {finding["rule"] for finding in report["findings"]} == {
        "identical_error",
        "repeated_test_failure",
        "repeated_tool_outcome",
    }
    assert all(len(finding["evidence_ids"]) == 3 for finding in report["findings"])
    assert "stall" not in str(report).lower()


def test_diff_oscillation_is_a_signal_with_uncertain_attribution():
    findings = diff_oscillations(
        [
            {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one"},
            {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two"},
            {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one"},
        ]
    )

    assert findings == [
        {
            "rule": "diff_oscillation",
            "rule_version": "wd-010.v1",
            "evidence_ids": ["a", "b", "c"],
            "count": 3,
            "attribution": "uncertain",
            "explanation": (
                "Observed A-to-B-to-A Git diff fingerprints; concurrent edits are not attributable."
            ),
        }
    ]


def test_git_diff_fingerprint_uses_the_hidden_process_runner(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"diff", stderr=b"")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("git_diff_fingerprint bypassed the hidden runner")

    monkeypatch.setattr("agent_watchdog.analysis._run", fake_run)
    monkeypatch.setattr("agent_watchdog.analysis.subprocess.run", unexpected_run)

    assert git_diff_fingerprint(tmp_path) == (hashlib.sha256(b"diff").hexdigest(), 4)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["git", "-C"]
    assert kwargs == {"capture_output": True, "timeout": 0.5, "check": False}


def test_report_pairs_tool_boundaries_for_observed_durations():
    project = uuid4()
    start = datetime(2026, 9, 7, tzinfo=UTC)
    events = [
        Envelope(
            provider="codex",
            project_id=project,
            session_id="session-1",
            kind="tool.start",
            source="hook",
            received_at=start,
            payload={"codex": {"tool_use_id": "call-1"}},
        ),
        Envelope(
            provider="codex",
            project_id=project,
            session_id="session-1",
            kind="tool.finish",
            source="hook",
            received_at=start + timedelta(seconds=2),
            payload={
                "codex": {
                    "tool_use_id": "call-1",
                    "content": {"tool_response": {"exit_code": 0}},
                }
            },
        ),
    ]

    assert analyze(events)["metrics"]["tool_durations"] == {
        "observed_count": 1,
        "total_seconds": 2.0,
        "max_seconds": 2.0,
    }
