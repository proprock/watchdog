"""WD-012 calibration tooling: reproducible sampling, review, and report math."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog import inspection
from agent_watchdog.analysis import finding_fingerprint
from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.daemon import _drain_controls, mutate_registry
from agent_watchdog.events import Envelope
from agent_watchdog.storage import StorageError, Store

FINGERPRINT_LENGTH = 64


@pytest.fixture
def calibrate():
    path = Path(__file__).parents[1] / "scripts" / "calibrate.py"
    spec = importlib.util.spec_from_file_location("calibrate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(project_id, session_id, kind, moment, *, provider="codex", payload=None, checkout=None):
    return Envelope(
        event_id=uuid4(),
        provider=provider,
        project_id=project_id,
        checkout_id=checkout,
        session_id=session_id,
        kind=kind,
        received_at=moment,
        source="hook",
        payload={provider: payload} if payload else {},
    )


def repeated_tool_calls(project_id, session_id, start, *, count=3, checkout=None):
    """Three identical failing tool outcomes trip the repeated_tool_outcome rule."""
    events = []
    for index in range(count):
        moment = start + timedelta(seconds=index * 10)
        events.append(
            event(
                project_id,
                session_id,
                "tool.start",
                moment,
                payload={"tool_name": "shell", "tool_input": {"command": "uv run pytest"}},
                checkout=checkout,
            )
        )
        events.append(
            event(
                project_id,
                session_id,
                "tool.finish",
                moment + timedelta(seconds=1),
                payload={
                    "tool_name": "shell",
                    "tool_input": {"command": "uv run pytest"},
                    "tool_response": {"exit_code": 1},
                },
                checkout=checkout,
            )
        )
    return events


@pytest.fixture
def capture(tmp_path):
    """A registered project holding one noisy session and several quiet ones."""
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    root = tmp_path / "checkout"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    start = datetime.now(UTC) - timedelta(days=1)
    with Store(paths.project_data(project.id), project.id) as store:
        for stored in repeated_tool_calls(project.id, "noisy", start):
            store.put(stored)
        store.put(event(project.id, "noisy", "session.end", start + timedelta(minutes=5)))
        for index in range(4):
            session_id = f"quiet-{index}"
            for step in range(6):
                store.put(
                    event(
                        project.id,
                        session_id,
                        "turn.start",
                        start + timedelta(minutes=index, seconds=step),
                    )
                )
            store.put(
                event(project.id, session_id, "session.end", start + timedelta(minutes=index + 1))
            )
        # A short-lived session that must never reach a reviewer.
        store.put(event(project.id, "trivial", "session.start", start))
    return paths, config, project


def sample_args(calibrate, paths, output, **overrides):
    arguments = [
        "sample",
        "--home",
        str(paths.config.parent),
        "--project",
        "checkout",
        "--min-events",
        "5",
        "--target",
        "3",
        "--seed",
        "7",
        "--output",
        str(output),
    ]
    for name, value in overrides.items():
        option = f"--{name.replace('_', '-')}"
        arguments += [option] if value is True else [option, str(value)]
    return calibrate.build_parser().parse_args(arguments)


def test_the_sample_freezes_findings_and_excludes_trivial_sessions(calibrate, capture, tmp_path):
    paths, _, _ = capture
    args = sample_args(calibrate, paths, tmp_path / "sample.json")

    sample = calibrate.build_sample(paths, args)

    identities = {record["session_id"] for record in sample["sessions"]}
    assert "noisy" in identities
    assert "trivial" not in identities
    assert sample["selection"]["skipped"]["below_min_events"] == 1
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    assert [finding["rule"] for finding in noisy["findings"]] == [
        "identical_error",
        "repeated_tool_outcome",
    ]
    assert len(noisy["findings"][0]["fingerprint"]) == FINGERPRINT_LENGTH


def test_the_sample_keeps_every_firing_session_and_draws_the_rest(calibrate, capture, tmp_path):
    paths, _, _ = capture
    args = sample_args(calibrate, paths, tmp_path / "sample.json")

    sample = calibrate.build_sample(paths, args)

    strata = sample["selection"]["strata"]
    assert strata["with_finding"] == 1
    assert strata["without_finding_available"] == 4
    # Silent sessions are what make a false negative countable.
    assert strata["without_finding_drawn"] == 2
    assert len(sample["sessions"]) == 3


def test_all_eligible_includes_every_silent_session_independent_of_target_and_seed(
    calibrate, capture, tmp_path
):
    paths, _, _ = capture
    args = sample_args(
        calibrate,
        paths,
        tmp_path / "sample.json",
        all_eligible=True,
        target=1,
        seed=999,
    )

    sample = calibrate.build_sample(paths, args)

    identities = {record["session_id"] for record in sample["sessions"]}
    assert identities == {"noisy", *(f"quiet-{index}" for index in range(4))}
    assert sample["selection"]["all_eligible"] is True
    assert sample["selection"]["strata"]["without_finding_drawn"] == 4


def test_the_same_seed_selects_the_same_cohort(calibrate, capture, tmp_path):
    paths, _, _ = capture
    first = calibrate.build_sample(paths, sample_args(calibrate, paths, tmp_path / "a.json"))
    second = calibrate.build_sample(paths, sample_args(calibrate, paths, tmp_path / "b.json"))
    other = calibrate.build_sample(
        paths, sample_args(calibrate, paths, tmp_path / "c.json", seed=999)
    )

    def identities(sample):
        return [record["session_id"] for record in sample["sessions"]]

    assert identities(first) == identities(second)
    assert set(identities(other)) <= set(identities(first)) | {
        f"quiet-{index}" for index in range(4)
    }


def test_the_csv_view_matches_the_frozen_manifest(calibrate, capture, tmp_path):
    import csv

    paths, _, _ = capture
    output = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, output))

    csv_path = calibrate.write_sample(sample, output)

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["session_id"] for row in rows] == [
        record["session_id"] for record in sample["sessions"]
    ]
    noisy = next(row for row in rows if row["session_id"] == "noisy")
    assert noisy["finding_count"] == "2"
    assert noisy["rules"] == "identical_error repeated_tool_outcome"


def annotate_session(paths, config, project, session_id, **fields):
    request_id = str(uuid4())
    directory = paths.data / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "action": "label",
                "project_id": str(project.id),
                "provider": "codex",
                "session_id": session_id,
                "outcome": fields.pop("outcome", "unknown"),
                **fields,
            }
        ),
        encoding="utf-8",
    )
    assert _drain_controls(paths, config) is True


def record_verdict(paths, config, project, session_id, finding, verdict):
    request_id = str(uuid4())
    directory = paths.data / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "action": "verdict",
                "project_id": str(project.id),
                "provider": "codex",
                "session_id": session_id,
                "rule": finding["rule"],
                "rule_version": finding["rule_version"],
                "fingerprint": finding["fingerprint"],
                "verdict": verdict,
                "note": None,
            }
        ),
        encoding="utf-8",
    )
    assert _drain_controls(paths, config) is True


def report_args(calibrate, paths, sample_path, output):
    return calibrate.build_parser().parse_args(
        [
            "report",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
            "--output",
            str(output),
        ]
    )


def test_precision_reports_its_denominator_and_keeps_uncertain_separate(
    calibrate, capture, tmp_path
):
    paths, config, project = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    repeated = next(
        finding for finding in noisy["findings"] if finding["rule"] == "repeated_tool_outcome"
    )
    record_verdict(paths, config, project, "noisy", repeated, "true_positive")

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    stats = report["session_scoped_rules"]["repeated_tool_outcome"]
    assert stats["observed"] == 1 and stats["true_positive"] == 1
    assert stats["precision"] == 1.0 and stats["precision_denominator"] == 1
    assert stats["uncertain"] == 0


def test_a_rule_with_no_observation_stays_null_rather_than_perfect(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    unobserved = report["checkout_scoped_rules"]["rules"]["diff_oscillation"]
    assert unobserved["precision"] is None
    assert unobserved["reason"] == "no observation in this dataset; null is not zero"


def test_a_stuck_session_without_a_finding_is_counted_as_a_false_negative(
    calibrate, capture, tmp_path
):
    paths, config, project = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    quiet = next(record for record in sample["sessions"] if record["session_id"] != "noisy")
    annotate_session(
        paths,
        config,
        project,
        quiet["session_id"],
        outcome="abandoned",
        progress_state="stuck",
        reviewer_note="repeated the same edit by hand",
    )

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    assert report["false_negatives"]["count"] == 1
    missed = report["false_negatives"]["sessions"][0]
    assert missed["session_id"] == quiet["session_id"]
    assert missed["reviewer_note"] == "repeated the same edit by hand"
    assert report["session_states"]["stuck"] == 1


def test_the_report_records_the_dataset_digest_and_supersedes_the_first_pass(
    calibrate, capture, tmp_path
):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    assert report["dataset"]["sample_sha256"] == calibrate.digest(sample_path)
    assert report["supersedes"]["format_version"] == "wd-012.calibration.v1"
    assert "Null is not zero" in report["hook_overhead"]["note"]
    assert report["hook_overhead"]["in_hook_ms"]["p50"] is None


def test_the_markdown_report_states_unmeasured_rules(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    markdown = calibrate.render_markdown(report)

    assert "# WD-012 calibration report" in markdown
    assert "null" in markdown
    assert report["dataset"]["sample_sha256"] in markdown


def test_a_rule_below_the_precision_gate_is_not_recommended(calibrate, capture, tmp_path):
    paths, config, project = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    repeated = next(
        finding for finding in noisy["findings"] if finding["rule"] == "repeated_tool_outcome"
    )
    record_verdict(paths, config, project, "noisy", repeated, "false_positive")

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    lines = report["recommendations"]
    assert any(
        line.startswith("repeated_tool_outcome: not recommended") and "0.00%" in line
        for line in lines
    )
    assert any("no rule cleared" in line.lower() for line in lines)
    assert "repeated_tool_outcome: not recommended" in calibrate.render_markdown(report)


def test_a_rule_at_the_precision_gate_is_recommended(calibrate, capture, tmp_path):
    paths, config, project = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    repeated = next(
        finding for finding in noisy["findings"] if finding["rule"] == "repeated_tool_outcome"
    )
    record_verdict(paths, config, project, "noisy", repeated, "true_positive")

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    lines = report["recommendations"]
    assert any(
        line.startswith("repeated_tool_outcome: recommended") and "n=1" in line for line in lines
    )
    assert "repeated_tool_outcome: recommended" in calibrate.render_markdown(report)


@pytest.mark.parametrize("reviewed,uncertain", [(1, 0), (2, 1)])
def test_high_reviewed_precision_cannot_pass_with_missing_or_uncertain_verdicts(
    calibrate, reviewed, uncertain
):
    stats = {
        "observed": 2,
        "reviewed": reviewed,
        "true_positive": 1,
        "false_positive": 0,
        "uncertain": uncertain,
        "precision": 1.0,
        "precision_denominator": 1,
    }

    recommendations = calibrate.recommend({"repeated_tool_outcome": stats})

    assert recommendations[0].startswith("repeated_tool_outcome: not recommended")
    assert "cannot clear the gate" in recommendations[0]
    assert "No rule cleared" in recommendations[-1]


def test_an_unobserved_rule_is_not_recommended(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    lines = report["recommendations"]
    assert any(
        line.startswith("repeated_test_failure: not recommended") and "n=0" in line
        for line in lines
    )


def test_the_reviewer_navigates_and_stops_without_writing(
    calibrate, capture, tmp_path, monkeypatch, capsys
):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    answers = iter(["n", "b", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr(
        calibrate, "submit", lambda *_: pytest.fail("navigation must not write anything")
    )
    args = calibrate.build_parser().parse_args(
        [
            "annotate",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
        ]
    )

    assert calibrate.annotate(paths, args) == 0

    printed = capsys.readouterr().out
    assert "[ 1/3 ]" in printed and "[ 2/3 ]" in printed
    assert "findings: frozen sample of" in printed
    assert "verdicts attach to these fingerprints" in printed


def test_the_card_shows_captured_work_not_only_counts(
    calibrate, capture, tmp_path, monkeypatch, capsys
):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    position = next(
        index
        for index, record in enumerate(sample["sessions"], start=1)
        if record["session_id"] == "noisy"
    )
    answers = iter(["j", str(position), "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    args = calibrate.build_parser().parse_args(
        [
            "annotate",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
        ]
    )

    assert calibrate.annotate(paths, args) == 0

    printed = capsys.readouterr().out
    assert "repeated x3" in printed
    assert "uv run pytest" in printed
    assert "repeated_tool_outcome" in printed


def test_content_is_flattened_to_one_readable_line(calibrate):
    assert calibrate.one_line("  many\n  spaced\tlines ") == "many spaced lines"
    assert calibrate.one_line("x" * 400).endswith("...")
    assert len(calibrate.one_line("x" * 400)) == 300
    # Reviewed text is the reviewer's own, in any language; it must survive intact.
    assert calibrate.one_line("проверка") == "проверка"
    assert calibrate.one_line({"command": "uv run pytest"}) == '{"command": "uv run pytest"}'


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(StorageError("Unknown session in the selected provider"), id="raised"),
        pytest.param({"ok": False, "message": "Invalid control request"}, id="rejected"),
    ],
)
def test_a_failed_acknowledgement_is_reported_to_the_reviewer(
    calibrate, capture, monkeypatch, capsys, outcome
):
    paths, _, _ = capture

    def control(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # Never reach the real control path here: request_control starts a daemon.
    monkeypatch.setattr(calibrate.daemon, "request_control", control)

    recorded = calibrate.submit(paths, {"action": "verdict"})

    assert recorded is False
    printed = capsys.readouterr().out
    assert "NOT recorded" in printed
    assert ("Unknown session" in printed) or ("Invalid control request" in printed)


def turn_and_tool(project_id, session_id, start):
    """One prompt, one failing shell call, and one reply, with content retained."""
    return [
        event(
            project_id,
            session_id,
            "turn.start",
            start,
            payload={"content": {"prompt": "run the tests"}},
        ),
        event(
            project_id,
            session_id,
            "tool.start",
            start + timedelta(seconds=5),
            payload={
                "tool_name": "shell",
                "tool_use_id": "call-1",
                "content": {"tool_input": {"command": "uv run pytest"}},
            },
        ),
        event(
            project_id,
            session_id,
            "tool.finish",
            start + timedelta(seconds=17),
            payload={
                "tool_name": "shell",
                "tool_use_id": "call-1",
                "content": {
                    "tool_input": {"command": "uv run pytest"},
                    "tool_response": {"exit_code": 1},
                },
                "metadata": {"tool_duration_ms": 12000, "error": "exit status 1"},
            },
        ),
        event(
            project_id,
            session_id,
            "turn.end",
            start + timedelta(seconds=20),
            payload={"content": {"last_assistant_message": "the import is wrong"}},
        ),
    ]


def test_the_timeline_collapses_a_tool_call_into_one_readable_row(calibrate):
    start = datetime.now(UTC)
    events = turn_and_tool(uuid4(), "session", start)

    rows = calibrate.build_timeline(events)

    assert [row["label"] for row in rows] == ["prompt", "shell", "reply"]
    call = rows[1]
    assert call["event_ids"] == [str(events[1].event_id), str(events[2].event_id)]
    assert call["state"] == "FAILED"
    assert "exit 1" in call["note"] and "12.0s" in call["note"]
    assert call["text"] == "uv run pytest"
    line = calibrate.timeline_line(call)
    assert line.startswith("  0:00:05") and "FAILED" in line


def test_an_unfinished_tool_call_says_no_result_was_observed(calibrate):
    start = datetime.now(UTC)
    events = turn_and_tool(uuid4(), "session", start)[:2]

    rows = calibrate.build_timeline(events)

    assert rows[1]["state"] == "no result observed"


def test_capture_counts_separate_an_empty_reply_from_one_that_was_not_stored(calibrate):
    project_id, start = uuid4(), datetime.now(UTC)
    events = [
        event(
            project_id,
            "session",
            "turn.end",
            start,
            payload={"content": {"last_assistant_message": "done"}},
        ),
        event(
            project_id,
            "session",
            "turn.end",
            start + timedelta(seconds=1),
            payload={"content": {"last_assistant_message": ""}},
        ),
        event(
            project_id, "session", "turn.end", start + timedelta(seconds=2), payload={"content": {}}
        ),
        event(
            project_id,
            "session",
            "turn.end",
            start + timedelta(seconds=3),
            payload={"content": "omitted"},
        ),
    ]

    counts = calibrate.capture_status(events)

    assert counts["replies"] == 4
    assert counts["replies_stored"] == 1
    assert counts["replies_empty"] == 1
    assert counts["replies_not_stored"] == 2
    assert counts["omitted_events"] == 1
    assert calibrate.capture_is_complete(counts) is False
    line = calibrate.capture_line(counts)
    assert "replies 1/4, 1 empty, 2 not stored" in line
    assert "content omitted on 1 events" in line


def test_a_fully_captured_session_reports_no_missing_content(calibrate):
    counts = calibrate.capture_status(turn_and_tool(uuid4(), "session", datetime.now(UTC)))

    assert calibrate.capture_is_complete(counts) is True
    assert "results 1/1" in calibrate.capture_line(counts)


def test_the_card_says_when_a_live_re_analysis_left_the_frozen_list_behind(calibrate):
    frozen = [{"fingerprint": "aaa"}, {"fingerprint": "bbb"}]
    live = [{"fingerprint": "aaa"}, {"fingerprint": "ccc"}]

    assert calibrate.drift_line(frozen, frozen) == "live re-analysis agrees with this frozen list"
    drifted = calibrate.drift_line(frozen, live)
    assert "1 new since the sample" in drifted
    assert "1 no longer produced" in drifted
    assert "verdicts stay on the frozen list" in drifted


def test_finding_evidence_is_shown_as_work_not_as_event_identifiers(calibrate, capsys):
    start = datetime.now(UTC)
    events = turn_and_tool(uuid4(), "session", start)
    rows = calibrate.build_timeline(events)
    rows_by_event = {event_id: row for row in rows for event_id in row["event_ids"]}
    missing = str(uuid4())
    finding = {"evidence_ids": [str(events[2].event_id), missing]}

    calibrate.print_evidence(finding, rows_by_event)

    printed = capsys.readouterr().out
    assert "uv run pytest" in printed and "FAILED" in printed
    assert str(events[2].event_id) not in printed
    assert missing not in printed
    assert f"{missing[:8]}  not in the current store" in printed


def test_the_live_detail_is_readable_and_never_dumps_event_identifiers(
    calibrate, capture, tmp_path, capsys
):
    paths, _, project = capture
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, tmp_path / "sample.json"))
    record = next(item for item in sample["sessions"] if item["session_id"] == "noisy")
    report = inspection.report(paths, project, session_id="noisy", provider="codex")

    calibrate.render_detail(report, record)

    printed = capsys.readouterr().out
    assert "LIVE re-analysis, not the frozen sample" in printed
    assert all(event_id not in printed for event_id in report["event_ids"])
    assert "in the frozen sample" in printed
    assert "task_outcome_unknown" in printed


def test_the_review_resumes_at_the_first_unlabelled_session(calibrate, capture, tmp_path):
    paths, config, project = capture
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, tmp_path / "sample.json"))
    records = sample["sessions"]
    assert len(records) > 1

    assert calibrate.first_unreviewed(paths, project, records) == 0

    annotate_session(paths, config, project, records[0]["session_id"], outcome="success")

    assert calibrate.first_unreviewed(paths, project, records) == 1


@pytest.mark.parametrize("name", ("payload.exe", "script.ps1"))
def test_a_transcript_path_from_a_trace_is_never_launched_by_its_suffix(
    calibrate, tmp_path, capsys, name
):
    planted = tmp_path / name
    planted.write_text("not a transcript", encoding="utf-8")

    calibrate.open_observed_path(planted)

    assert "Refusing to open" in capsys.readouterr().out


def test_a_transcript_path_that_no_longer_exists_is_reported(calibrate, tmp_path, capsys):
    calibrate.open_observed_path(tmp_path / "gone.jsonl")

    assert "no longer exists" in capsys.readouterr().out


def test_a_tool_result_that_was_not_stored_falls_back_to_the_provider_error(calibrate):
    start = datetime.now(UTC)
    events = [
        event(
            uuid4(),
            "session",
            "tool.finish",
            start,
            payload={
                "tool_name": "shell",
                "content": {"tool_input": {"command": "cargo build"}},
                "metadata": {"error": "the harness killed the command"},
            },
        )
    ]

    row = calibrate.build_timeline(events)[0]

    assert row["state"] == "result not stored"
    assert "error the harness killed the command" in row["note"]


def test_usage_events_stay_out_of_the_timeline(calibrate):
    project_id, start = uuid4(), datetime.now(UTC)
    events = [
        event(
            project_id,
            "session",
            "turn.start",
            start,
            payload={"content": {"prompt": "keep going"}},
        ),
        event(
            project_id,
            "session",
            "usage",
            start + timedelta(seconds=1),
            payload={"usage": {"delta": {"input_tokens": 12}}},
        ),
    ]

    rows = calibrate.build_timeline(events)

    assert [row["label"] for row in rows] == ["prompt"]


def tool_finish(project_id, response, **payload):
    return event(
        project_id,
        "session",
        "tool.finish",
        datetime.now(UTC),
        payload={
            "tool_name": "Bash",
            "content": {"tool_input": {"command": "ls"}, "tool_response": response},
            **payload,
        },
    )


def test_a_result_without_an_exit_code_still_shows_what_came_back(calibrate):
    """A provider that reports no exit code leaves the text as the only evidence."""
    response = {"stdout": "3 files changed", "stderr": "", "interrupted": False}

    row = calibrate.build_timeline([tool_finish(uuid4(), response)])[0]

    assert row["state"] == "result unclear"
    assert "3 files changed" in row["note"]


def test_a_present_but_empty_output_is_not_reported_as_missing(calibrate):
    row = calibrate.build_timeline([tool_finish(uuid4(), {"stdout": "", "stderr": ""})])[0]

    assert row["note"] == "no output"


def test_an_exit_code_alone_adds_no_excerpt(calibrate):
    row = calibrate.build_timeline([tool_finish(uuid4(), {"exit_code": 2})])[0]

    assert row["state"] == "FAILED"
    assert row["note"] == "exit 2"


def test_a_failing_result_shows_its_error_text(calibrate):
    response = {"stdout": "", "stderr": "ModuleNotFoundError: widgets", "exit_code": 1}

    row = calibrate.build_timeline([tool_finish(uuid4(), response)])[0]

    assert row["state"] == "FAILED"
    assert "exit 1" in row["note"]
    assert "ModuleNotFoundError: widgets" in row["note"]


def test_the_review_keeps_earlier_answers_when_the_next_key_is_pressed(
    calibrate, capture, tmp_path, monkeypatch
):
    """One keystroke sets one field; it must not silently clear the others."""
    paths, config, project = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    position = next(
        index
        for index, record in enumerate(sample["sessions"], start=1)
        if record["session_id"] == "noisy"
    )
    answers = iter(["j", str(position), "t", "feature", "o", "1", "p", "3", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    def drain(_paths, request):
        # Apply the control inbox directly: request_control would start a daemon.
        request_id = str(uuid4())
        directory = paths.data / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{request_id}.json").write_text(
            json.dumps({"schema_version": 1, "request_id": request_id, **request}),
            encoding="utf-8",
        )
        assert _drain_controls(paths, config) is True
        return {"ok": True}

    monkeypatch.setattr(calibrate.daemon, "request_control", drain)
    args = calibrate.build_parser().parse_args(
        [
            "annotate",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
        ]
    )

    assert calibrate.annotate(paths, args) == 0

    label = calibrate.load_state(paths, project, "codex", "noisy")["label"]
    assert label["task_outcome"] == "success"
    assert label["task_type"] == "feature"
    assert label["progress_state"] == "stuck"


def test_captured_output_naming_a_failure_hook_is_not_counted_as_one(calibrate):
    """Tool output is data: a session that prints the hook name did not fail."""
    project_id, start = uuid4(), datetime.now(UTC)
    events = [
        event(
            project_id,
            "session",
            "tool.finish",
            start,
            payload={
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "content": {
                    "tool_input": {"command": "uv run pytest"},
                    "tool_response": "tests/test_hooks.py::test_a_PostToolUseFailure_is_observed",
                },
            },
        ),
        event(
            project_id,
            "session",
            "tool.finish",
            start + timedelta(seconds=1),
            payload={
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "content": {"tool_input": {"command": "cargo build"}, "tool_response": "denied"},
            },
        ),
        event(
            project_id,
            "session",
            "tool.finish",
            start + timedelta(seconds=2),
            payload={
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "content": {"tool_input": {"command": "ls"}, "tool_response": {"exit_code": 1}},
            },
        ),
    ]

    assert calibrate.summarize(events)["tool_failures"] == 2


def test_the_card_reports_how_the_tool_calls_ended(calibrate):
    rows = calibrate.build_timeline(turn_and_tool(uuid4(), "session", datetime.now(UTC)))

    assert calibrate.result_line(rows, 1) == "FAILED 1"
    assert "the frozen sample recorded 4 failed" in calibrate.result_line(rows, 4)


def test_the_suggest_key_prints_a_model_opinion_and_records_nothing(
    calibrate, capture, tmp_path, monkeypatch, capsys
):
    paths, _, _ = capture
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    calibrate.write_sample(sample, sample_path)
    answer = {
        "summary": "The same test keeps failing.",
        "judgement": {
            "state": "stuck",
            "rationale": "Three identical failing runs with no edit between them.",
            "unblock": "Read the failure before re-running.",
            "confidence": "medium",
            "evidence_ids": [],
        },
        "recommendations": [],
        "rule_candidates": [],
    }
    requests = []

    def runner(request):
        requests.append(request)
        return calibrate.llm.Result(answer, None, {})

    monkeypatch.setattr(calibrate, "suggestion_runner", lambda _paths: runner)
    monkeypatch.setattr(
        calibrate, "submit", lambda *_: pytest.fail("a suggestion must not write anything")
    )
    answers = iter(["s", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    args = calibrate.build_parser().parse_args(
        [
            "annotate",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
        ]
    )

    assert calibrate.annotate(paths, args) == 0

    printed = capsys.readouterr().out
    assert "LLM suggestion, not a verdict - nothing was recorded" in printed
    assert "state      stuck  confidence medium  (ungrounded" in printed
    assert "next step  Read the failure before re-running." in printed
    (request,) = requests
    assert "Mode: session" in request.system_prompt


@pytest.fixture
def guarded(capture):
    """The capture project plus a Claude session in which a control action fired."""
    paths, config, project = capture
    start = datetime.now(UTC) - timedelta(hours=20)
    with Store(paths.project_data(project.id), project.id) as store:
        for step in range(5):
            store.put(
                event(
                    project.id,
                    "guarded",
                    "turn.start",
                    start + timedelta(seconds=step),
                    provider="claude",
                )
            )
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id="guarded",
                kind="control",
                source="daemon",
                received_at=start + timedelta(seconds=10),
                payload={
                    "claude": {
                        "rule": "destructive_command",
                        "rule_version": "2",
                        "action": "ask",
                        "reason": "Watchdog: confirm.",
                        "text": "Watchdog: confirm.",
                        "evidence_ids": [str(uuid4())],
                    }
                },
            )
        )
        store.put(
            event(
                project.id,
                "guarded",
                "session.end",
                start + timedelta(minutes=1),
                provider="claude",
            )
        )
    return capture


def test_a_control_finding_is_sampled_annotated_and_kept_out_of_the_precision_gate(
    calibrate, guarded, tmp_path, monkeypatch
):
    paths, config, project = guarded
    sample_path = tmp_path / "sample.json"
    # A session whose only finding is a control action is not a shadow-finding
    # session, so the seeded draw may skip it; take every eligible one.
    sample = calibrate.build_sample(
        paths, sample_args(calibrate, paths, sample_path, all_eligible=True)
    )
    calibrate.write_sample(sample, sample_path)
    position = next(
        index
        for index, record in enumerate(sample["sessions"], start=1)
        if record["session_id"] == "guarded"
    )
    record = sample["sessions"][position - 1]
    (finding,) = record["findings"]
    assert (finding["rule"], finding["action"], finding["rule_version"]) == (
        "destructive_command",
        "ask",
        "2",
    )

    # j <n>: jump to the session, 1: its first finding, 1: true_positive, Enter: no note.
    answers = iter(["j", str(position), "1", "1", "", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    def drain(_paths, request):
        # Apply the control inbox directly: request_control would start a daemon.
        request_id = str(uuid4())
        directory = paths.data / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{request_id}.json").write_text(
            json.dumps({"schema_version": 1, "request_id": request_id, **request}),
            encoding="utf-8",
        )
        assert _drain_controls(paths, config) is True
        return {"ok": True}

    monkeypatch.setattr(calibrate.daemon, "request_control", drain)
    args = calibrate.build_parser().parse_args(
        [
            "annotate",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
        ]
    )
    assert calibrate.annotate(paths, args) == 0

    with inspection.database(paths, project) as db:
        (recorded,) = inspection.session_verdicts(db, "claude", "guarded")
    assert (recorded["rule"], recorded["rule_version"], recorded["verdict"]) == (
        "destructive_command",
        "2",
        "true_positive",
    )
    assert recorded["fingerprint"] == finding["fingerprint"]

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    assert "destructive_command" not in report["session_scoped_rules"]
    assert not any("destructive_command" in line for line in report["recommendations"])
    stats = report["action_rules"]["rules"]["destructive_command"]
    assert (stats["observed"], stats["true_positive"], stats["precision"]) == (1, 1, 1.0)


def test_a_session_with_only_a_control_finding_is_not_a_shadow_finding_session(
    calibrate, guarded, tmp_path
):
    paths, _, _ = guarded
    sample = calibrate.build_sample(
        paths, sample_args(calibrate, paths, tmp_path / "sample.json", target=1, all_eligible=True)
    )
    sample_path = tmp_path / "sample.json"
    calibrate.write_sample(sample, sample_path)

    report = calibrate.build_report(paths, report_args(calibrate, paths, sample_path, tmp_path))

    # Only the noisy session fired a shadow rule; the guarded one counts as silent.
    assert report["confusion"].get("unlabeled/finding") == 1
    assert report["confusion"].get("unlabeled/no_finding", 0) >= 1


def recompute_args(calibrate, paths, sample_path, *extra):
    return calibrate.build_parser().parse_args(
        [
            "report",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
            "--rule-version",
            "wd-010.v2",
            *extra,
        ]
    )


def frozen_v1_sample(calibrate, paths, tmp_path):
    """The capture cohort as a v1 freeze: a real finding plus one v2 will not reproduce."""
    sample_path = tmp_path / "sample.json"
    sample = calibrate.build_sample(paths, sample_args(calibrate, paths, sample_path))
    by_id = {record["session_id"]: record for record in sample["sessions"]}

    def as_v1(finding):
        return finding | {
            "rule_version": "wd-010.v1",
            "fingerprint": finding_fingerprint(
                finding["rule"], "wd-010.v1", finding["evidence_ids"]
            ),
        }

    by_id["noisy"]["findings"] = [as_v1(f) for f in by_id["noisy"]["findings"]]
    stale_evidence = [str(uuid4()) for _ in range(3)]
    stale = {
        "rule": "repeated_tool_outcome",
        "rule_version": "wd-010.v1",
        "evidence_ids": stale_evidence,
        "count": 3,
        "fingerprint": finding_fingerprint("repeated_tool_outcome", "wd-010.v1", stale_evidence),
        "attribution": "observed",
        "explanation": "v1 only",
    }
    quiet = next(record for record in sample["sessions"] if record["session_id"] != "noisy")
    quiet["findings"] = [stale]
    sample["rule_version"] = "wd-010.v1"
    calibrate.write_sample(sample, sample_path)
    return sample_path, sample, quiet, stale


def test_report_recomputes_under_a_rule_version_and_never_transfers_verdicts(
    calibrate, capture, tmp_path
):
    paths, config, project = capture
    sample_path, sample, quiet, stale = frozen_v1_sample(calibrate, paths, tmp_path)
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    repeated = next(f for f in noisy["findings"] if f["rule"] == "repeated_tool_outcome")
    record_verdict(paths, config, project, "noisy", repeated, "true_positive")
    record_verdict(paths, config, project, quiet["session_id"], stale, "false_positive")

    report = calibrate.build_report(paths, recompute_args(calibrate, paths, sample_path))

    comparison = report["version_comparison"]["rules"]["repeated_tool_outcome"]
    assert comparison["v1_observed"] == 2
    assert (comparison["v1_survived"], comparison["v1_dropped"]) == (1, 1)
    assert comparison["dropped_false_positive"] == 1
    assert comparison.get("dropped_true_positive", 0) == 0
    assert comparison["v2_observed"] == 1
    assert report["dataset"]["rule_version"] == "wd-010.v2"
    assert report["dataset"]["recomputed_from"] == "wd-010.v1"
    # The v1 true positive does not carry over: the v2 finding is unreviewed.
    stats = report["session_scoped_rules"]["repeated_tool_outcome"]
    assert stats["observed"] == 1 and stats["reviewed"] == 0 and stats["precision"] is None


def test_a_review_record_supplies_the_old_versions_verdicts(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path, _, _, stale = frozen_v1_sample(calibrate, paths, tmp_path)
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {"findings": [{"fingerprint": stale["fingerprint"], "verdict": "false_positive"}]}
        ),
        encoding="utf-8",
    )

    report = calibrate.build_report(
        paths, recompute_args(calibrate, paths, sample_path, "--review", str(review))
    )

    comparison = report["version_comparison"]["rules"]["repeated_tool_outcome"]
    assert comparison["dropped_false_positive"] == 1


def test_a_cohort_session_without_stored_events_is_listed_not_scored(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path, sample, _, _ = frozen_v1_sample(calibrate, paths, tmp_path)
    ghost = dict(sample["sessions"][0]) | {"session_id": "ghost", "findings": []}
    sample["sessions"].append(ghost)
    calibrate.write_sample(sample, sample_path)

    report = calibrate.build_report(paths, recompute_args(calibrate, paths, sample_path))

    assert report["excluded_sessions"]["events_missing"] == [f"{ghost['provider']}/ghost"]
    assert report["dataset"]["sessions"] == len(sample["sessions"]) - 1


def test_only_the_current_rule_version_can_be_recomputed(calibrate, capture, tmp_path):
    paths, _, _ = capture
    sample_path, _, _, _ = frozen_v1_sample(calibrate, paths, tmp_path)
    args = recompute_args(calibrate, paths, sample_path)
    args.rule_version = "wd-010.v1"

    with pytest.raises(SystemExit):
        calibrate.build_report(paths, args)


def test_events_added_after_the_freeze_do_not_change_the_recomputation(
    calibrate, capture, tmp_path
):
    paths, _, project = capture
    sample_path, sample, _, _ = frozen_v1_sample(calibrate, paths, tmp_path)
    noisy = next(record for record in sample["sessions"] if record["session_id"] == "noisy")
    after = datetime.fromisoformat(noisy["last_received_at"]) + timedelta(minutes=1)
    with Store(paths.project_data(project.id), project.id) as store:
        for offset in range(3):
            moment = after + timedelta(seconds=offset)
            store.put(
                event(
                    project.id,
                    "noisy",
                    "tool.finish",
                    moment,
                    payload={
                        "tool_name": "shell",
                        "tool_input": {"command": "uv run ruff check"},
                        "tool_response": {"exit_code": 1},
                    },
                )
            )

    report = calibrate.build_report(paths, recompute_args(calibrate, paths, sample_path))

    assert report["excluded_sessions"]["event_count_changed"] == []
    assert report["version_comparison"]["rules"]["repeated_tool_outcome"]["v2_observed"] == 1


def test_the_recomputed_cohort_is_written_as_a_sample_to_annotate(
    calibrate, capture, tmp_path, capsys
):
    paths, _, _ = capture
    sample_path, _, _, _ = frozen_v1_sample(calibrate, paths, tmp_path)

    code = calibrate.main(
        [
            "report",
            "--home",
            str(paths.config.parent),
            "--project",
            "checkout",
            "--sample",
            str(sample_path),
            "--rule-version",
            "wd-010.v2",
        ]
    )

    assert code == 0
    written = json.loads(capsys.readouterr().out)
    recomputed = json.loads((tmp_path / "sample-v2-sample.json").read_text(encoding="utf-8"))
    assert written["sample"] == str(tmp_path / "sample-v2-sample.json")
    assert (tmp_path / "sample-v2-calibration.json").exists()
    assert recomputed["rule_version"] == "wd-010.v2"
    assert recomputed["recomputed_from"] == "wd-010.v1"
    assert all(
        finding["rule_version"] == "wd-010.v2"
        for record in recomputed["sessions"]
        for finding in record["findings"]
    )
