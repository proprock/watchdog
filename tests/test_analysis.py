import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog import inspection
from agent_watchdog.analysis import (
    CHECKOUT_DEADLINE_SECONDS,
    POLICY_RULE_VERSION,
    REPORT_SCHEMA_VERSION,
    CheckoutUnknown,
    analyze,
    control_findings,
    diff_oscillations,
    finding_fingerprint,
    git_diff_fingerprint,
    model_family_matches,
    normalize_error_text,
    same_model_subagent_spawn,
    tool_outcome,
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


def control_event(project, moment, *, rule="destructive_command", hook_event=None, **fields):
    recorded = {
        "rule": rule,
        "rule_version": "3",
        "action": "ask",
        "reason": "Watchdog: confirm this.",
        "text": "Watchdog: confirm this.",
        "evidence_ids": [str(hook_event or uuid4())],
        "delivered_at": moment.isoformat(),
    } | fields
    return Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        kind="control",
        source="daemon",
        received_at=moment,
        payload={"claude": recorded},
    )


def test_a_control_action_is_a_finding_with_the_hook_event_as_evidence():
    project, moment, hook_event = uuid4(), datetime(2026, 10, 7, tzinfo=UTC), uuid4()
    event = control_event(project, moment, hook_event=hook_event)

    (finding,) = control_findings([event])

    assert finding["rule"] == "destructive_command"
    assert finding["rule_version"] == "3"
    assert finding["action"] == "ask"
    assert finding["count"] == 1
    assert finding["attribution"] == "observed"
    assert finding["explanation"] == "Watchdog: confirm this."
    assert sorted(finding["evidence_ids"]) == sorted([str(event.event_id), str(hook_event)])
    assert finding["fingerprint"] == finding_fingerprint(
        "destructive_command", "3", finding["evidence_ids"]
    )


def test_each_firing_is_its_own_finding_and_a_replay_keeps_the_fingerprint():
    project, moment = uuid4(), datetime(2026, 10, 7, tzinfo=UTC)
    events = [control_event(project, moment + timedelta(seconds=n)) for n in range(2)]

    first = control_findings(events)
    again = control_findings(list(reversed(events)))

    assert len(first) == 2 and len({item["fingerprint"] for item in first}) == 2
    assert sorted(item["fingerprint"] for item in again) == sorted(
        item["fingerprint"] for item in first
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rule": "x"},
        {"rule": "", "rule_version": "1", "action": "ask"},
        {"rule": "x", "rule_version": "1", "action": 5},
        {"rule": "x", "rule_version": "1", "action": "ask", "evidence_ids": "not-a-list"},
    ],
)
def test_a_malformed_control_payload_is_skipped_not_fatal(payload):
    project, moment = uuid4(), datetime(2026, 10, 7, tzinfo=UTC)
    event = control_event(project, moment).model_copy(update={"payload": {"claude": payload}})

    assert control_findings([event]) == []


def test_other_event_kinds_are_not_control_findings():
    project, moment = uuid4(), datetime(2026, 10, 7, tzinfo=UTC)

    assert control_findings([agent_start(project, moment, agent_id="a")]) == []


def test_analyze_reports_control_findings_next_to_the_others():
    project, moment = uuid4(), datetime(2026, 10, 7, tzinfo=UTC)

    report = analyze([control_event(project, moment)])

    assert [item["rule"] for item in report["findings"]] == ["destructive_command"]


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
    assert report["rule_version"] == "wd-010.v2"
    assert report["timeline"]["wall_seconds"] == 6.0
    assert report["metrics"]["compactions"] == {"started": 1, "completed": 0}
    assert report["metrics"]["tool_outcomes"] == {
        "failure": 3,
        "interrupt": 0,
        "success": 1,
        "unknown": 0,
    }
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
            "rule_version": "wd-010.v2",
            "evidence_ids": ["a", "b", "c"],
            "count": 3,
            "fingerprint": finding_fingerprint("diff_oscillation", "wd-010.v2", ["a", "b", "c"]),
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
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("git_diff_fingerprint bypassed the hidden runner")

    monkeypatch.setattr("agent_watchdog.analysis._run", fake_run)
    monkeypatch.setattr("agent_watchdog.analysis.subprocess.run", unexpected_run)

    assert isinstance(git_diff_fingerprint(tmp_path), tuple)
    assert [command[5] for command, _ in calls] == ["diff", "diff", "ls-files"]
    for command, kwargs in calls:
        # Without this setting `git diff` rewrites stale stat data in the index.
        assert command[:5] == ["git", "-c", "diff.autoRefreshIndex=false", "-C", str(tmp_path)]
        assert kwargs.keys() == {"capture_output", "timeout", "check"}
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert 0 < kwargs["timeout"] <= CHECKOUT_DEADLINE_SECONDS
    assert "--cached" not in calls[0][0]
    assert "--cached" in calls[1][0]
    assert all("--no-renames" in command for command, _ in calls[:2])


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    # Host Git configuration (excludes, autocrlf, external diff) must not change
    # what a fingerprint means on any CI platform.
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Slow CI runners must not turn the behavioral tests into deadline tests.
    monkeypatch.setattr("agent_watchdog.analysis.CHECKOUT_DEADLINE_SECONDS", 30.0)
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "tracked.txt").write_bytes(b"base\n")
    (root / ".gitignore").write_bytes(b"ignored.txt\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def fingerprint(root):
    result = git_diff_fingerprint(root)
    assert isinstance(result, tuple)
    return result[0]


def unknown(result):
    assert isinstance(result, CheckoutUnknown)
    return result


def unknown_reason(result):
    return unknown(result).reason


def stage_change(root):
    (root / "tracked.txt").write_bytes(b"staged\n")
    git(root, "add", "tracked.txt")
    assert git(root, "diff") == "", "the change must be staged only"


def unstaged_change(root):
    (root / "tracked.txt").write_bytes(b"unstaged\n")


def untracked_file(root):
    (root / "new.txt").write_bytes(b"new\n")


def revert_tracked(root):
    git(root, "checkout", "HEAD", "--", "tracked.txt")


def remove_untracked(root):
    (root / "new.txt").unlink()


@pytest.mark.parametrize(
    ("apply", "revert"),
    [
        pytest.param(unstaged_change, revert_tracked, id="unstaged"),
        pytest.param(stage_change, revert_tracked, id="staged"),
        pytest.param(untracked_file, remove_untracked, id="untracked"),
    ],
)
def test_checkout_fingerprint_yields_oscillation_evidence_for_each_state(checkout, apply, revert):
    observed = [fingerprint(checkout)]
    apply(checkout)
    observed.append(fingerprint(checkout))
    revert(checkout)
    observed.append(fingerprint(checkout))

    findings = diff_oscillations(
        {"checkout_id": "c", "fingerprint": value, "snapshot_id": f"s{index}"}
        for index, value in enumerate(observed)
    )

    assert [finding["rule"] for finding in findings] == ["diff_oscillation"]


def test_checkout_fingerprint_distinguishes_mixed_states_and_never_reads_clean(checkout):
    states = {"clean": fingerprint(checkout)}
    for name, prepare in {
        "staged": stage_change,
        "unstaged": unstaged_change,
        "untracked": untracked_file,
    }.items():
        prepare(checkout)
        states[name] = fingerprint(checkout)
        revert_tracked(checkout)
        (checkout / "new.txt").unlink(missing_ok=True)
    stage_change(checkout)
    (checkout / "tracked.txt").write_bytes(b"staged then edited\n")
    states["staged and unstaged"] = fingerprint(checkout)

    assert len(set(states.values())) == len(states)


def test_checkout_fingerprint_is_stable_for_an_unchanged_state(checkout):
    untracked_file(checkout)
    stage_change(checkout)

    assert git_diff_fingerprint(checkout) == git_diff_fingerprint(checkout)


def test_checkout_fingerprint_ignores_ignored_files_and_sees_renames(checkout):
    clean = fingerprint(checkout)

    (checkout / "ignored.txt").write_bytes(b"ignored\n")
    assert fingerprint(checkout) == clean

    git(checkout, "mv", "tracked.txt", "renamed.txt")
    assert fingerprint(checkout) != clean


def test_checkout_fingerprint_hashes_binary_untracked_content(checkout):
    (checkout / "blob.bin").write_bytes(b"\x00\xff\x00one")
    first = fingerprint(checkout)
    (checkout / "blob.bin").write_bytes(b"\x00\xff\x00two")

    assert fingerprint(checkout) != first


def test_checkout_fingerprint_hashes_untracked_symlinks_without_following_them(checkout):
    try:
        (checkout / "link").symlink_to("missing-target")
    except OSError:
        pytest.skip("symlinks are unavailable on this host")
    first = fingerprint(checkout)
    (checkout / "link").unlink()
    (checkout / "link").symlink_to("other-target")

    assert fingerprint(checkout) != first


def test_checkout_fingerprint_is_read_only(checkout):
    clean = fingerprint(checkout)
    # A stale stat entry is what tempts `git diff` into rewriting the index.
    os.utime(checkout / "tracked.txt", (1, 1))
    objects = checkout / ".git" / "objects"
    index_before = (checkout / ".git" / "index").read_bytes()
    objects_before = sorted(path.name for path in objects.rglob("*"))

    assert fingerprint(checkout) == clean  # a touched, unchanged file is not a change

    assert (checkout / ".git" / "index").read_bytes() == index_before
    assert sorted(path.name for path in objects.rglob("*")) == objects_before
    assert not (checkout / ".git" / "index.lock").exists()


def test_checkout_fingerprint_covers_staged_work_before_the_first_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("agent_watchdog.analysis.CHECKOUT_DEADLINE_SECONDS", 30.0)
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-b", "main")
    empty = fingerprint(root)

    (root / "first.txt").write_bytes(b"first\n")
    git(root, "add", "first.txt")

    assert git(root, "diff") == ""
    assert fingerprint(root) != empty


def test_checkout_fingerprint_is_unknown_outside_a_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()

    assert unknown_reason(git_diff_fingerprint(plain)) == "git_failed"


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        ("MAX_UNTRACKED_FILES", "too_many_untracked"),
        ("MAX_UNTRACKED_BYTES", "untracked_too_large"),
        ("MAX_DIFF_BYTES", "diff_too_large"),
    ],
)
def test_checkout_fingerprint_is_unknown_when_a_bound_is_exceeded(
    checkout, monkeypatch, limit, reason
):
    (checkout / "one.txt").write_bytes(b"one\n")
    (checkout / "two.txt").write_bytes(b"two\n")
    (checkout / "tracked.txt").write_bytes(b"changed\n")
    assert isinstance(git_diff_fingerprint(checkout), tuple)

    monkeypatch.setattr(f"agent_watchdog.analysis.{limit}", 1)

    assert unknown_reason(git_diff_fingerprint(checkout)) == reason


def test_checkout_fingerprint_reports_git_failure_and_timeout_separately(checkout, monkeypatch):
    def failing(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, stdout=b"", stderr=b"fatal: broken")

    def timing_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("agent_watchdog.analysis._run", failing)
    result = unknown(git_diff_fingerprint(checkout))
    assert result.reason == "git_failed"
    assert "128" in result.detail and "fatal: broken" in result.detail
    monkeypatch.setattr("agent_watchdog.analysis._run", timing_out)
    assert unknown_reason(git_diff_fingerprint(checkout)) == "deadline"


def test_checkout_fingerprint_reports_an_exhausted_deadline(checkout, monkeypatch):
    monkeypatch.setattr("agent_watchdog.analysis.CHECKOUT_DEADLINE_SECONDS", -1.0)

    assert unknown_reason(git_diff_fingerprint(checkout)) == "deadline"


def test_checkout_fingerprint_reports_a_deadline_hit_while_hashing_untracked_content(
    checkout, monkeypatch
):
    untracked_file(checkout)
    # Skip the Git reads so only the file hashing sees the already-expired deadline.
    monkeypatch.setattr(
        "agent_watchdog.analysis._git_output",
        lambda checkout, args, deadline: b"new.txt\0" if args[0] == "ls-files" else b"",
    )
    monkeypatch.setattr("agent_watchdog.analysis.CHECKOUT_DEADLINE_SECONDS", -1.0)

    assert unknown_reason(git_diff_fingerprint(checkout)) == "deadline"


def test_checkout_fingerprint_is_unknown_when_an_untracked_file_cannot_be_read(
    checkout, monkeypatch
):
    untracked_file(checkout)

    def unreadable(*args, **kwargs):
        raise PermissionError("locked by another process")

    monkeypatch.setattr("agent_watchdog.analysis.open", unreadable, raising=False)

    result = unknown(git_diff_fingerprint(checkout))
    assert result.reason == "untracked_unreadable"
    assert "locked by another process" in result.detail


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


# WD-146: tool outcomes and the wd-010.v2 statistical rules.

START = datetime(2026, 10, 8, tzinfo=UTC)


def claude_call(
    project,
    offset,
    *,
    name="Bash",
    tool_input="default",
    response=None,
    hook="PostToolUse",
    error=None,
    interrupt=None,
    agent_id=None,
    captured=True,
):
    content: dict = {}
    if captured:
        content["tool_input"] = {"command": "pytest"} if tool_input == "default" else tool_input
        if response is not None:
            content["tool_response"] = response
    payload: dict = {"hook_event_name": hook, "tool_name": name, "content": content}
    metadata = {
        key: value
        for key, value in (("error", error), ("is_interrupt", interrupt))
        if value is not None
    }
    if metadata:
        payload["metadata"] = metadata
    return Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        agent_id=agent_id,
        kind="tool.finish",
        source="hook",
        received_at=START + timedelta(seconds=offset),
        payload={"claude": payload},
    )


def claude_edit(project, offset, *, name="Edit", **overrides):
    return claude_call(
        project,
        offset,
        name=name,
        tool_input={"file_path": f"f{offset}.py"},
        response={"ok": True},
        **overrides,
    )


def claude_failure(project, offset, error, **overrides):
    return claude_call(project, offset, hook="PostToolUseFailure", error=error, **overrides)


def rules_of(report):
    return sorted(finding["rule"] for finding in report["findings"])


@pytest.mark.parametrize(
    ("provider", "payload", "expected"),
    [
        ("claude", {"hook_event_name": "PostToolUseFailure"}, "failure"),
        (
            "claude",
            {"hook_event_name": "PostToolUseFailure", "metadata": {"is_interrupt": False}},
            "failure",
        ),
        (
            "claude",
            {"hook_event_name": "PostToolUseFailure", "metadata": {"is_interrupt": True}},
            "interrupt",
        ),
        ("codex", {"hook_event_name": "PostToolUseFailure"}, "failure"),
        ("claude", {"hook_event_name": "PostToolUse"}, "success"),
        (
            "claude",
            {"hook_event_name": "PostToolUse", "content": {"tool_response": {"isError": True}}},
            "failure",
        ),
        (
            "claude",
            {"hook_event_name": "PostToolUse", "content": {"tool_response": {"exit_code": 2}}},
            "failure",
        ),
        ("claude", {"tool_name": "Bash"}, "unknown"),
        ("codex", {"hook_event_name": "PostToolUse"}, "unknown"),
        ("codex", {"content": {"tool_response": {"exit_code": 0}}}, "success"),
        ("codex", {"content": {"tool_response": {"exit_code": 1}}}, "failure"),
        ("codex", {"content": {"tool_response": {"output": "no code"}}}, "unknown"),
    ],
)
def test_tool_outcome_reads_the_provider_signals(provider, payload, expected):
    assert tool_outcome(payload, provider) == expected


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_finish_status_delegates_to_the_shared_classification(provider):
    payload = {"hook_event_name": "PostToolUseFailure", "metadata": {"is_interrupt": True}}
    event = Envelope(
        provider=provider,
        project_id=uuid4(),
        session_id="session-1",
        kind="tool.finish",
        source="hook",
        received_at=START,
        payload={provider: payload},
    )

    assert inspection.finish_status(event) == tool_outcome(payload, provider)


def test_a_claude_failure_hook_counts_as_a_failure_without_a_response():
    project = uuid4()

    report = analyze(
        [
            claude_failure(project, 1, "Exit code 1\nboom"),
            claude_call(project, 2, response={"stdout": "ok"}),
            claude_failure(project, 3, "interrupted", interrupt=True),
        ]
    )

    assert report["metrics"]["tool_outcomes"] == {
        "failure": 1,
        "interrupt": 1,
        "success": 1,
        "unknown": 0,
    }


def test_a_claude_call_without_a_hook_name_stays_unknown():
    project = uuid4()
    event = claude_call(project, 1, response={"stdout": "ok"}, hook="PreToolUse")

    assert analyze([event])["metrics"]["tool_outcomes"]["unknown"] == 1


def test_three_identical_claude_failures_trip_both_rules_despite_varying_paths():
    project = uuid4()
    errors = [
        "Exit code 1\nNo such file: C:\\work\\run-1\\a.py line 12",
        "Exit code 1\nNo such file: C:\\work\\run-2\\a.py line 98",
        "Exit code 1\nNo such file: C:\\work\\run-3\\a.py line 7",
    ]

    report = analyze(
        [claude_failure(project, number, error) for number, error in enumerate(errors, 1)]
    )

    assert rules_of(report) == ["identical_error", "repeated_tool_outcome"]
    assert report["metrics"]["tool_outcomes"]["failure"] == 3


def test_different_claude_errors_do_not_form_an_identical_error():
    project = uuid4()

    report = analyze(
        [
            claude_failure(project, 1, "Exit code 1\nNo such file: a.py"),
            claude_failure(project, 2, "Exit code 1\nPermission denied"),
            claude_failure(project, 3, "Exit code 2\nsyntax error"),
        ]
    )

    assert "identical_error" not in rules_of(report)


def test_an_interrupt_is_not_an_identical_error():
    project = uuid4()

    report = analyze(
        [claude_failure(project, number, "stopped", interrupt=True) for number in (1, 2, 3)]
    )

    assert "identical_error" not in rules_of(report)
    assert report["metrics"]["tool_outcomes"]["failure"] == 0


def test_edits_between_runs_with_changing_output_are_progress_not_repetition():
    project = uuid4()
    events = []
    for number in range(3):
        events.append(
            claude_call(project, number * 2, response={"stdout": f"{88 + number} passed"})
        )
        events.append(claude_edit(project, number * 2 + 1))

    assert "repeated_tool_outcome" not in rules_of(analyze(events))


def test_a_run_chain_broken_by_an_edit_and_new_output_stays_short():
    project = uuid4()

    report = analyze(
        [
            claude_call(project, 1, response={"stdout": "2 failed"}),
            claude_call(project, 2, response={"stdout": "2 failed"}),
            claude_edit(project, 3),
            claude_call(project, 4, response={"stdout": "1 failed"}),
        ]
    )

    assert "repeated_tool_outcome" not in rules_of(report)


def test_identical_output_still_counts_across_edits():
    project = uuid4()
    git_status = {"tool_input": {"command": "git status"}, "response": {"stdout": "clean"}}
    events = []
    for number in range(3):
        events.append(
            claude_call(
                project,
                number * 2,
                tool_input=git_status["tool_input"],
                response=git_status["response"],
            )
        )
        events.append(claude_edit(project, number * 2 + 1))

    report = analyze(events)

    assert rules_of(report) == ["repeated_tool_outcome"]
    assert "same output" in report["findings"][0]["explanation"]


def test_repeats_without_any_edit_count_even_when_the_output_changes():
    project = uuid4()

    report = analyze(
        [
            claude_call(project, number, response={"stdout": f"tick {number}"})
            for number in (1, 2, 3)
        ]
    )

    assert rules_of(report) == ["repeated_tool_outcome"]
    assert "no edits between" in report["findings"][0]["explanation"]


def test_an_edit_by_another_agent_in_the_session_breaks_the_edit_free_chain():
    project = uuid4()

    report = analyze(
        [
            claude_call(project, 1, response={"stdout": "a"}),
            claude_edit(project, 2, agent_id="subagent"),
            claude_call(project, 3, response={"stdout": "b"}),
            claude_call(project, 4, response={"stdout": "c"}),
        ]
    )

    assert "repeated_tool_outcome" not in rules_of(report)


def test_repeats_are_counted_per_agent():
    project = uuid4()
    events = [
        claude_call(project, number, response={"stdout": "same"}, agent_id=agent)
        for number, agent in enumerate(("a", "b", "a", "b", "a", "b"), 1)
    ]

    assert rules_of(analyze(events)) == ["repeated_tool_outcome", "repeated_tool_outcome"]
    assert rules_of(analyze(events[:4])) == []


def test_calls_without_captured_input_are_not_grouped():
    project = uuid4()

    report = analyze([claude_call(project, number, captured=False) for number in (1, 2, 3)])

    assert report["findings"] == []
    assert report["metrics"]["tool_outcomes"]["success"] == 3


def test_codex_exit_code_fixtures_keep_their_v1_behaviour():
    project = uuid4()

    report = analyze(
        [
            tool(project, START + timedelta(seconds=number), code=1, output="boom")
            for number in (1, 2, 3)
        ]
    )

    assert rules_of(report) == ["identical_error", "repeated_tool_outcome"]


def test_normalize_error_text_drops_paths_numbers_and_quoted_values():
    text = "Error in 'foo' at C:\\a\\b.py:12 and /tmp/x/y.py: code 0xdeadbeef"

    assert normalize_error_text(text) == "Error in '<s>' at <path>:<n> and <path>: code <hex>"


def oscillation(offsets):
    return [
        {
            "snapshot_id": name,
            "checkout_id": "c",
            "fingerprint": fingerprint,
            "observed_at": (START + timedelta(seconds=offset)).isoformat(),
        }
        for (name, fingerprint), offset in zip(
            (("a", "one"), ("b", "two"), ("c", "one")), offsets, strict=True
        )
    ]


def turn_start(project, offset):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id="session-1",
        kind="turn.start",
        source="hook",
        received_at=START + timedelta(seconds=offset),
    )


def test_an_oscillation_needs_an_edit_or_turn_between_each_pair_of_snapshots():
    project = uuid4()
    snapshots = oscillation((0, 10, 20))

    caused = diff_oscillations(snapshots, [claude_edit(project, 5), turn_start(project, 15)])
    only_first_step = diff_oscillations(snapshots, [claude_edit(project, 5)])
    nothing_between = diff_oscillations(snapshots, [claude_edit(project, 25)])
    only_reads = diff_oscillations(
        snapshots, [claude_call(project, 5, response={"stdout": "x"}), turn_start(project, 30)]
    )

    assert [f["evidence_ids"] for f in caused] == [["a", "b", "c"]]
    assert only_first_step == []
    assert nothing_between == []
    assert only_reads == []


def test_an_oscillation_is_not_filtered_when_the_evidence_to_filter_is_missing():
    snapshots = oscillation((0, 10, 20))
    undated = [{key: value for key, value in s.items() if key != "observed_at"} for s in snapshots]

    assert len(diff_oscillations(snapshots)) == 1
    assert len(diff_oscillations(snapshots, [])) == 0
    assert len(diff_oscillations(undated, [])) == 1


def test_analyze_applies_the_oscillation_gate_to_its_own_events():
    project = uuid4()
    snapshots = oscillation((0, 10, 20))
    quiet = [claude_call(project, 5, response={"stdout": "x"})]
    busy = [claude_edit(project, 5), claude_edit(project, 15)]

    assert "diff_oscillation" not in rules_of(analyze(quiet, snapshots=snapshots))
    assert "diff_oscillation" in rules_of(analyze(busy, snapshots=snapshots))
