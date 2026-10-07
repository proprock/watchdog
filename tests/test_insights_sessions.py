"""Offline contracts for `insights sessions`, the project session triage (WD-133)."""

import json
from datetime import timedelta

import pytest
from helpers.insights import runner_for, store
from test_insights_modes import NONE, call, event, request
from test_insights_session import _cli
from test_storage_v6 import BASE

from agent_watchdog import insights
from agent_watchdog.insights import bundle, contract, llm, render, sessions
from agent_watchdog.inspection import database
from agent_watchdog.storage import StorageError, Store


def _usage(session, offset, request_id, occupancy):
    return request(offset, request_id, occupancy).model_copy(update={"session_id": session})


def _calm(session="calm", at=0):
    return [
        event("session.start", at, session=session, metadata={"source": "startup"}),
        event(
            "turn.start",
            at + 1,
            session=session,
            content={"prompt": "Add the export flag"},
            metadata={},
        ),
        _usage(session, at + 2, f"{session}-r1", 20_000),
        call(at + 3, "git status", session=session),
        event(
            "turn.end",
            at + 4,
            session=session,
            content={"last_assistant_message": "Flag added."},
            metadata={},
        ),
        event("session.end", at + 5, session=session, metadata={}),
    ]


def _failing(offset, session, command="uv run pytest -q"):
    return call(
        offset,
        command,
        session=session,
        hook_event_name="PostToolUseFailure",
        response={"exit_code": 1, "stderr": "AssertionError: boom"},
        metadata={"error": "Exit code 1\nAssertionError: boom"},
    )


def _stuck(session="stuck", at=100, failures=4):
    return [
        event("session.start", at, session=session, metadata={"source": "startup"}),
        event(
            "turn.start",
            at + 1,
            session=session,
            content={"prompt": "Fix the flaky parser test"},
            metadata={},
        ),
        _usage(session, at + 2, f"{session}-r1", 30_000),
        *[_failing(at + 3 + index, session) for index in range(failures)],
        _usage(session, at + 3 + failures, f"{session}-r2", 36_000),
    ]


def _middling(session="middling", at=200):
    return [
        event("session.start", at, session=session, metadata={"source": "startup"}),
        event(
            "turn.start",
            at + 1,
            session=session,
            content={"prompt": "Rename the module"},
            metadata={},
        ),
        _failing(at + 2, session, "uv run ruff check ."),
        call(at + 3, "uv run ruff format .", session=session),
        event(
            "turn.end",
            at + 4,
            session=session,
            content={"last_assistant_message": "Renamed."},
            metadata={},
        ),
    ]


def _build(tmp_path, events):
    paths, project = store(tmp_path, events)
    return paths, project, sessions.build(paths, project, **NONE)


def _by_session(draft):
    return {item["session_id"]: item for item in draft.items}


def test_a_facet_holds_the_task_the_trouble_signals_and_the_evidence(tmp_path):
    _paths, _project, draft = _build(tmp_path, [*_calm(), *_stuck()])
    stuck = _by_session(draft)["stuck"]

    assert stuck["first_prompt"] == "Fix the flaky parser test"
    assert stuck["last_assistant_message"] is None
    assert (stuck["turns"], stuck["tool_calls"], stuck["tool_failures"]) == (1, 4, 4)
    assert stuck["ended"] is False
    assert stuck["peak_tokens"] == 36_000
    assert stuck["compactions"] == 0
    assert {finding["rule"] for finding in stuck["findings"]} >= {"repeated_tool_outcome"}
    assert stuck["loops"]["count"] == 1
    assert stuck["loops"]["longest"] == 4
    assert set(stuck["signals"]) == {"findings", "loops", "failures", "no_end"}
    assert stuck["label"]["task_outcome"] == "unknown"

    calm = _by_session(draft)["calm"]
    assert calm["last_assistant_message"] == "Flag added."
    assert calm["ended"] is True
    assert calm["signals"] == []
    assert calm["findings"] == []

    known = bundle.evidence_ids(draft.items)
    assert {stuck["item_id"], calm["item_id"]} <= known
    assert set(stuck["evidence_ids"]) <= known
    assert len(stuck["evidence_ids"]) >= 3


def test_findings_are_computed_per_session_not_across_the_project(tmp_path):
    # Two failing calls in each of two sessions total four identical failures,
    # but no single session reaches the repetition threshold.
    events = [
        *[_failing(index, "a") for index in range(2)],
        *[_failing(10 + index, "b") for index in range(2)],
    ]
    _paths, _project, draft = _build(tmp_path, events)
    assert all(item["findings"] == [] for item in draft.items)


def test_only_attributed_shadow_findings_count_as_trouble(tmp_path, monkeypatch):
    def finding(rule, session_ids):
        return {
            "rule": rule,
            "count": 3,
            "evidence_ids": ["e1", "e2", "e3", "e4"],
            "session_ids": session_ids,
        }

    monkeypatch.setattr(
        sessions.analysis,
        "analyze",
        lambda events, snapshots=(): {
            "findings": [
                finding("diff_oscillation", ["calm"]),
                finding("diff_oscillation", []),
                finding("same_model_subagent_spawn", []),
            ]
        },
    )
    _paths, _project, draft = _build(tmp_path, _calm())

    (item,) = draft.items
    assert [entry["rule"] for entry in item["findings"]] == ["diff_oscillation"]
    assert item["findings"][0]["evidence_ids"] == ["e1", "e2", "e3"]
    assert item["unattributed_oscillations"] == 1
    assert item["signals"] == ["findings"]
    assert draft.coverage["sessions_with_unattributed_oscillations"] == 1


def test_only_snapshots_taken_during_the_session_can_implicate_it(tmp_path, monkeypatch):
    seen = []
    moments = {
        "during": BASE + timedelta(seconds=3),
        "long_after": BASE + timedelta(days=3),
    }
    monkeypatch.setattr(
        sessions.inspection,
        "snapshots_for_checkouts",
        lambda db, checkouts: [
            {"snapshot_id": name, "observed_at": moment.isoformat(), "session_ids": []}
            for name, moment in moments.items()
        ],
    )
    monkeypatch.setattr(
        sessions.analysis,
        "analyze",
        lambda events, snapshots=(): (
            seen.extend(item["snapshot_id"] for item in snapshots) or {"findings": []}
        ),
    )

    _build(tmp_path, _calm())

    assert seen == ["during"]


def test_sessions_rank_by_trouble_and_the_calmest_come_last(tmp_path):
    _paths, _project, draft = _build(tmp_path, [*_calm(), *_middling(), *_stuck()])

    assert [item["session_id"] for item in draft.items] == ["stuck", "middling", "calm"]
    assert [item["item_id"] for item in draft.items] == ["S1", "S2", "S3"]
    assert draft.facts["sessions_total"] == 3
    assert draft.facts["sessions_with_trouble"] == 1
    assert draft.facts["sessions_ended"] == 1


def test_a_recorded_label_and_the_unknowns_are_reported(tmp_path):
    paths, project, _draft = _build(tmp_path, [*_calm(), *_stuck()])
    with Store(paths.project_data(project.id), project.id) as writer:
        writer.label("claude", "stuck", outcome="abandoned", task_type="bugfix")

    draft = sessions.build(paths, project, **NONE)
    stuck = _by_session(draft)["stuck"]
    assert stuck["label"]["task_outcome"] == "abandoned"
    assert draft.facts["sessions_labelled"] == 1
    assert draft.coverage["sessions_without_end"] == 1
    assert draft.coverage["sessions_unlabelled"] == 1
    assert draft.coverage["sessions_without_usage"] == 0
    assert any("unknown is not zero" in note.lower() for note in draft.coverage["notes"])


def test_a_session_without_captured_content_stays_unknown_not_empty(tmp_path):
    events = [
        event("session.start", 0, session="bare", metadata={}),
        event("turn.start", 1, session="bare", metadata={}),
    ]
    _paths, _project, draft = _build(tmp_path, events)
    (item,) = draft.items
    assert item["first_prompt"] is None
    assert draft.coverage["sessions_without_prompt"] == 1
    assert draft.coverage["sessions_without_usage"] == 1


def test_the_window_bounds_the_sessions(tmp_path):
    paths, project = store(tmp_path, [*_calm(at=0), *_stuck(at=100_000)])
    since = BASE + timedelta(seconds=50_000)
    draft = sessions.build(paths, project, provider=None, session_id=None, since=since, until=None)
    assert [item["session_id"] for item in draft.items] == ["stuck"]


def test_the_budget_cut_drops_the_calmest_sessions_first(tmp_path):
    _paths, _project, draft = _build(tmp_path, [*_calm(), *_middling(), *_stuck()])
    window = {"since": None, "until": None, "provider": None, "session_id": None}
    frame = bundle.fit(
        bundle.Draft(draft.facts, draft.coverage, []),
        mode="sessions",
        window=window,
        max_tokens=10**6,
    )
    frame_bytes = len(bundle.dumps(frame).encode("utf-8")) + 200
    one = len(bundle.dumps(draft.items[0]).encode("utf-8")) + 2
    max_tokens = int((frame_bytes + one) / bundle.BYTES_PER_TOKEN) + 1

    fitted = bundle.fit(draft, mode="sessions", window=window, max_tokens=max_tokens)

    assert [item["session_id"] for item in fitted["items"]] == ["stuck"]
    assert fitted["coverage"]["truncated"]["items"] == 2
    assert fitted["facts"]["sessions_total"] == 3


_run = runner_for("sessions", since=BASE - timedelta(days=1))


def _answer(item, evidence):
    return {
        "summary": "One session looks stuck.",
        "candidates": [
            {
                "state": "stuck",
                "title": "The parser test keeps failing",
                "rationale": "The same pytest failure repeats four times.",
                "item_ids": [item],
                "suggestion": "Open it and ask for a different approach.",
                "confidence": "high",
                "evidence_ids": evidence,
            }
        ],
        "patterns": [
            {
                "title": "Abandoned after the same failure",
                "kind": "abandoned_after_failure",
                "description": "Sessions end unfinished after a repeated test failure.",
                "item_ids": [item, "S2"],
                "confidence": "low",
                "evidence_ids": evidence,
            }
        ],
        "recommendations": [],
        "rule_candidates": [],
    }


def test_an_answer_is_validated_grounded_resolved_and_rendered(tmp_path):
    paths, project, draft = _build(tmp_path, [*_calm(), *_middling(), *_stuck()])
    top = draft.items[0]
    cited = [top["item_id"], top["evidence_ids"][0]]
    output = tmp_path / "sessions.md"

    result = _run(
        paths,
        project,
        lambda request: llm.Result(_answer(top["item_id"], cited), None, {}),
        output=output,
    )

    assert result["status"] == "ok"
    (candidate,) = result["candidates"]
    assert candidate["ungrounded"] is False
    assert candidate["sessions"] == [{"provider": "claude", "session_id": "stuck"}]
    assert candidate["open_with"] == (
        "agent-watchdog insights session --project repo --provider claude --session stuck"
    )
    (pattern,) = result["patterns"]
    assert [entry["session_id"] for entry in pattern["sessions"]] == ["stuck", "middling"]
    text = output.read_text(encoding="utf-8")
    assert "## Candidate sessions" in text
    assert "## Patterns" in text
    assert "insights session --project repo --provider claude --session stuck" in text

    invented = _run(paths, project, lambda request: llm.Result(_answer("S99", ["nope"]), None, {}))
    assert invented["candidates"][0]["ungrounded"] is True
    assert invented["candidates"][0]["sessions"] == []
    wrong = _answer(top["item_id"], cited)
    wrong["candidates"][0]["state"] = "progress"
    rejected = _run(paths, project, lambda request: llm.Result(wrong, None, {}))
    assert rejected["reason"] == "malformed_output"


def test_an_answer_changes_no_label_and_stores_nothing(tmp_path):
    paths, project, draft = _build(tmp_path, [*_calm(), *_stuck()])
    top = draft.items[0]

    def snapshot():
        with database(paths, project) as db:
            return (
                db.execute("SELECT COUNT(*) FROM events").fetchone(),
                db.execute("SELECT COUNT(*) FROM session_labels").fetchone(),
            )

    before = snapshot()
    _run(
        paths,
        project,
        lambda request: llm.Result(_answer(top["item_id"], [top["item_id"]]), None, {}),
    )
    assert snapshot() == before


def test_the_dry_run_sends_nothing_and_shows_the_bundle(tmp_path):
    paths, project = store(tmp_path, [*_calm(), *_stuck()])
    result = _run(
        paths, project, lambda request: pytest.fail("no model call in a dry run"), dry_run=True
    )
    assert result["status"] == "dry_run"
    assert [item["session_id"] for item in result["bundle"]["items"]] == ["stuck", "calm"]


def test_the_mode_covers_the_project_not_one_session_or_all_projects(tmp_path):
    paths, project = store(tmp_path, _calm())
    runner = lambda request: pytest.fail("no call for a refused scope")  # noqa: E731
    with pytest.raises(StorageError, match="whole project"):
        _run(paths, project, runner, provider="claude", session_id="calm")
    with pytest.raises(StorageError, match="--all-projects"):
        insights.run_all(
            paths,
            mode="sessions",
            provider=None,
            since=None,
            until=None,
            model="sonnet",
            effort=None,
            timeout=60.0,
            max_bundle_tokens=None,
            language="English",
            dry_run=True,
            output=None,
            runner=runner,
        )


def test_the_cli_offers_the_mode_and_refuses_one_session(tmp_path, monkeypatch, capsys):
    paths, project = store(tmp_path, [*_calm(), *_stuck()])
    since = (BASE - timedelta(days=1)).isoformat()
    args = ("insights", "sessions", "--project", str(project.id))

    assert _cli(monkeypatch, capsys, paths, *args, "--since", since, "--dry-run") == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "sessions"
    assert printed["facts"]["sessions_total"] == 2

    code = _cli(
        monkeypatch, capsys, paths, *args, "--provider", "claude", "--session", "calm", "--dry-run"
    )
    assert code == 1
    assert "whole project" in json.loads(capsys.readouterr().out)["message"]


def test_the_schema_is_self_contained_and_strict():
    schema = contract.json_schema(sessions.Output)
    assert "$ref" not in json.dumps(schema)
    for field in ("candidates", "patterns", "recommendations"):
        assert schema["properties"][field]["items"]["additionalProperties"] is False


def test_render_leaves_other_modes_without_triage_sections():
    text = render.markdown({"mode": "errors", "summary": "s", "recommendations": []})
    assert "Candidate sessions" not in text
    assert "## Patterns" not in text
