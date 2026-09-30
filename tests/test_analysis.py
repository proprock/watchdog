import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog.analysis import (
    POLICY_RULE_VERSION,
    REPORT_SCHEMA_VERSION,
    analyze,
    diff_oscillations,
    finding_fingerprint,
    git_diff_fingerprint,
    model_family_matches,
    same_model_subagent_spawn,
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


def claude_usage(
    project,
    moment,
    *,
    model,
    agent_id=None,
    occurred_at=None,
    received_at=None,
    request_id=None,
    response=None,
):
    payload: dict = {"model": model}
    if response is not None:
        # The claude-transcript-v1 reader's per-response shape.
        payload |= {"request_id": request_id, "usage": {"response": response}}
    return Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        agent_id=agent_id,
        native_event_id=request_id,
        kind="usage",
        source="transcript",
        received_at=received_at if received_at is not None else moment,
        occurred_at=occurred_at if occurred_at is not None else moment,
        payload={"claude": payload},
    )


def claude_response(**overrides):
    return {
        "input_tokens": 5,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20,
        "output_tokens": 30,
        "thinking_tokens": 7,
    } | overrides


def codex_usage(project, moment, *, delta, response_id=None):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id="session-1",
        kind="usage",
        source="transcript",
        native_event_id=response_id,
        received_at=moment,
        payload={"codex": {"usage": {"cumulative": delta, "delta": delta}}},
    )


def agent_start(project, moment, *, agent_id):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        agent_id=agent_id,
        kind="agent.start",
        source="hook",
        received_at=moment,
        payload={"claude": {"agent_type": "general-purpose"}},
    )


def test_same_model_subagent_spawn_is_logged():
    project = uuid4()
    start = datetime(2026, 9, 26, tzinfo=UTC)
    coordinator = claude_usage(project, start, model="claude-opus-5-5")
    spawn = agent_start(project, start + timedelta(seconds=1), agent_id="agent-1")
    subagent = claude_usage(
        project, start + timedelta(seconds=2), model="claude-opus-5-5", agent_id="agent-1"
    )
    findings = same_model_subagent_spawn([coordinator, spawn, subagent])
    assert len(findings) == 1
    finding = findings[0]
    assert finding["rule"] == "same_model_subagent_spawn"
    assert finding["rule_version"] == POLICY_RULE_VERSION
    assert finding["action"] == "both"
    # Evidence is the launch fact (agent.start) plus the matching usage event,
    # not the unrelated coordinator usage event that happened to establish
    # the compared model.
    assert sorted(finding["evidence_ids"]) == sorted([str(spawn.event_id), str(subagent.event_id)])
    assert finding["fingerprint"] == finding_fingerprint(
        "same_model_subagent_spawn", POLICY_RULE_VERSION, finding["evidence_ids"]
    )


def test_same_model_subagent_spawn_is_logged_once_per_agent_not_per_usage_event():
    project = uuid4()
    start = datetime(2026, 9, 26, tzinfo=UTC)
    coordinator = claude_usage(project, start, model="claude-opus-5-5")
    spawn = agent_start(project, start + timedelta(seconds=1), agent_id="agent-1")
    # A real subagent run can report several usage events (one per API
    # response); a live 3-way Explore run produced 8 usage rows for 3 spawns.
    usages = [
        claude_usage(
            project,
            start + timedelta(seconds=2 + offset),
            model="claude-opus-5-5",
            agent_id="agent-1",
        )
        for offset in range(3)
    ]
    findings = same_model_subagent_spawn([coordinator, spawn, *usages])
    assert len(findings) == 1


def test_different_model_subagent_spawn_is_not_logged():
    project = uuid4()
    start = datetime(2026, 9, 26, tzinfo=UTC)
    coordinator = claude_usage(project, start, model="claude-opus-5-5")
    subagent = claude_usage(
        project, start + timedelta(seconds=1), model="claude-haiku-4-5", agent_id="agent-1"
    )
    assert same_model_subagent_spawn([coordinator, subagent]) == []


def test_subagent_with_unresolved_model_is_not_compared():
    project = uuid4()
    start = datetime(2026, 9, 26, tzinfo=UTC)
    coordinator = claude_usage(project, start, model="claude-opus-5-5")
    subagent = Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        agent_id="agent-1",
        kind="usage",
        source="transcript",
        received_at=start + timedelta(seconds=1),
        payload={"claude": {"model": None}},
    )
    assert same_model_subagent_spawn([coordinator, subagent]) == []


def test_comparison_uses_occurred_at_not_ingestion_order():
    """A subagent transcript is only enriched after ``agent.end``, so its usage
    row can be *received* well before a later coordinator model switch even
    though it *occurred* after that switch. Only ``occurred_at`` order compares
    against the model genuinely active at the time.
    """
    project = uuid4()
    start = datetime(2026, 9, 26, tzinfo=UTC)
    # Ingested (received) first, but truly occurred last, after the switch.
    subagent = claude_usage(
        project,
        start,
        occurred_at=start + timedelta(seconds=30),
        received_at=start,
        model="claude-haiku-4-5",
        agent_id="agent-1",
    )
    coordinator_before = claude_usage(
        project,
        start,
        occurred_at=start + timedelta(seconds=10),
        received_at=start + timedelta(seconds=1),
        model="claude-opus-5-5",
    )
    coordinator_switch = claude_usage(
        project,
        start,
        occurred_at=start + timedelta(seconds=20),
        received_at=start + timedelta(seconds=2),
        model="claude-haiku-4-5",
    )
    findings = same_model_subagent_spawn([subagent, coordinator_before, coordinator_switch])
    assert len(findings) == 1
    assert str(subagent.event_id) in findings[0]["evidence_ids"]


@pytest.mark.parametrize(
    ("alias", "resolved_model", "expected"),
    [
        ("opus", "claude-opus-5-5", True),
        ("sonnet", "claude-sonnet-5", True),
        ("haiku", "claude-haiku-4-5-20251001", True),
        ("Opus", "claude-opus-5-5", True),
        ("opus", "claude-sonnet-5", False),
        ("sonnet", "claude-opus-5-5", False),
        ("son", "claude-sonnet-5", False),
    ],
)
def test_model_family_matches(alias, resolved_model, expected):
    assert model_family_matches(alias, resolved_model) is expected


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


def test_report_sums_codex_rollout_deltas():
    project = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    delta = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}

    report = analyze(
        [
            codex_usage(project, start, delta=delta),
            codex_usage(project, start + timedelta(seconds=1), delta=delta),
        ]
    )

    assert report["metrics"]["usage"] == {
        "records": 2,
        "deltas": {"cached_input_tokens": 120, "input_tokens": 200, "output_tokens": 20},
    }
    assert "usage_incomplete" not in report["gaps"]


def test_report_counts_a_codex_response_stored_twice_once():
    # WD-128: two spellings of one rollout path stored some responses twice.
    project = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    delta = {"input_tokens": 100, "output_tokens": 10}

    report = analyze(
        [
            codex_usage(project, start, delta=delta, response_id="resp-1"),
            codex_usage(project, start + timedelta(seconds=1), delta=delta, response_id="resp-1"),
            codex_usage(project, start + timedelta(seconds=2), delta=delta, response_id="resp-2"),
        ]
    )

    assert report["metrics"]["usage"] == {
        "records": 2,
        "deltas": {"input_tokens": 200, "output_tokens": 20},
    }


def test_report_sums_claude_responses_once_per_request():
    project = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    events = [
        claude_usage(project, start, model="m", request_id="req_1", response=claude_response()),
        claude_usage(
            project,
            start + timedelta(seconds=1),
            model="m",
            request_id="req_2",
            response=claude_response(input_tokens=3, cache_creation_input_tokens=0),
        ),
        # The same response observed twice counts once.
        claude_usage(
            project,
            start + timedelta(seconds=2),
            model="m",
            request_id="req_1",
            response=claude_response(),
        ),
    ]

    report = analyze(events)

    # Anthropic counters keep their raw values under the event_facts names, and
    # Anthropic reports no total, so none is synthesised.
    assert report["metrics"]["usage"] == {
        "records": 2,
        "deltas": {
            "cache_write_input_tokens": 20,
            "cached_input_tokens": 200,
            "input_tokens": 8,
            "output_tokens": 60,
            "reasoning_output_tokens": 14,
        },
    }
    assert "usage_incomplete" not in report["gaps"]


def test_claude_counter_missing_on_some_responses_is_unknown_not_partial():
    project = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    events = [
        claude_usage(project, start, model="m", request_id="req_1", response=claude_response()),
        claude_usage(
            project,
            start + timedelta(seconds=1),
            model="m",
            request_id="req_2",
            response=claude_response(thinking_tokens=None),
        ),
    ]

    report = analyze(events)

    # Real transcripts report thinking tokens on only some responses; a partial
    # sum would read as a complete total, so the counter stays unknown.
    assert "reasoning_output_tokens" not in report["metrics"]["usage"]["deltas"]
    assert report["metrics"]["usage"]["deltas"]["output_tokens"] == 60
    assert "usage_incomplete" not in report["gaps"]


@pytest.mark.parametrize(
    ("response", "unknown"),
    [
        (claude_response(input_tokens=None), "input_tokens"),
        (claude_response(cache_read_input_tokens=None), "cached_input_tokens"),
        (claude_response(output_tokens=None), "output_tokens"),
        ("not-a-counter-map", None),
    ],
)
def test_claude_usage_without_a_required_counter_is_incomplete(response, unknown):
    project = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)

    report = analyze(
        [claude_usage(project, start, model="m", request_id="req_1", response=response)]
    )

    assert "usage_incomplete" in report["gaps"]
    if unknown is not None:
        assert unknown not in report["metrics"]["usage"]["deltas"]


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
            "fingerprint": finding_fingerprint("diff_oscillation", "wd-010.v1", ["a", "b", "c"]),
            "attribution": "uncertain",
            "explanation": (
                "Observed A-to-B-to-A Git diff fingerprints; concurrent edits are not attributable."
            ),
            "session_ids": [],
        }
    ]


def test_diff_oscillation_attributes_to_the_sessions_active_when_captured():
    findings = diff_oscillations(
        [
            {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one", "session_ids": ["s1"]},
            {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two", "session_ids": ["s2"]},
            {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one", "session_ids": ["s1"]},
        ]
    )

    assert findings[0]["session_ids"] == ["s1", "s2"]
    assert findings[0]["attribution"] == "uncertain"


def test_analyze_only_surfaces_an_oscillation_for_its_implicated_sessions():
    project = uuid4()
    start = datetime(2026, 9, 16, tzinfo=UTC)
    snapshots = [
        {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one", "session_ids": ["s1"]},
        {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two", "session_ids": ["s1"]},
        {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one", "session_ids": ["s1"]},
    ]
    bystander = Envelope(
        provider="codex",
        project_id=project,
        session_id="bystander",
        kind="turn.start",
        source="hook",
        received_at=start,
    )

    implicated_report = analyze(
        [
            Envelope(
                provider="codex",
                project_id=project,
                session_id="s1",
                kind="turn.start",
                source="hook",
                received_at=start,
            )
        ],
        snapshots=snapshots,
    )
    bystander_report = analyze([bystander], snapshots=snapshots)

    assert [f["rule"] for f in implicated_report["findings"]] == ["diff_oscillation"]
    assert bystander_report["findings"] == []


def test_analyze_keeps_an_unattributable_oscillation_visible_to_every_session():
    project = uuid4()
    start = datetime(2026, 9, 16, tzinfo=UTC)
    snapshots = [
        {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one"},
        {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two"},
        {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one"},
    ]

    report = analyze(
        [
            Envelope(
                provider="codex",
                project_id=project,
                session_id="anyone",
                kind="turn.start",
                source="hook",
                received_at=start,
            )
        ],
        snapshots=snapshots,
    )

    assert [f["rule"] for f in report["findings"]] == ["diff_oscillation"]


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


def test_a_finding_carries_a_stable_fingerprint_over_its_evidence():
    first = diff_oscillations(
        [
            {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one"},
            {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two"},
            {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one"},
        ]
    )
    repeated = diff_oscillations(
        [
            {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one"},
            {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two"},
            {"snapshot_id": "c", "checkout_id": "c", "fingerprint": "one"},
        ]
    )
    wider = diff_oscillations(
        [
            {"snapshot_id": "a", "checkout_id": "c", "fingerprint": "one"},
            {"snapshot_id": "b", "checkout_id": "c", "fingerprint": "two"},
            {"snapshot_id": "d", "checkout_id": "c", "fingerprint": "one"},
        ]
    )

    assert len(first[0]["fingerprint"]) == 64
    assert first[0]["fingerprint"] == repeated[0]["fingerprint"]
    assert first[0]["fingerprint"] != wider[0]["fingerprint"]
