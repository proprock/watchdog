"""Offline contracts for `insights session`, the WD-014 second opinion (WD-126)."""

import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_insights_context_tokens import store
from test_insights_modes import call, event, request
from test_storage_v6 import BASE

from agent_watchdog import insights
from agent_watchdog.cli import main
from agent_watchdog.insights import llm, render, session
from agent_watchdog.storage import StorageError

ONE: dict[str, Any] = {"provider": "claude", "session_id": "s1", "since": None, "until": None}


def _session():
    return [
        event("session.start", 0, metadata={"source": "startup"}),
        event("turn.start", 1, content={"prompt": "Fix the flaky parser test"}, metadata={}),
        request(2, "r1", 30_000),
        call(
            3,
            "uv run pytest tests/test_parser.py",
            hook_event_name="PostToolUseFailure",
            metadata={"error": "Exit code 1\nAssertionError: parse('x') != 1"},
        ),
        request(4, "r2", 34_000),
        call(5, "uv run pytest tests/test_parser.py", metadata={"duration_ms": 1200}),
        event(
            "turn.end", 6, content={"last_assistant_message": "The test passes now."}, metadata={}
        ),
        event("turn.start", 10, content={"prompt": "Now update the docs"}, metadata={}),
        request(11, "r3", 40_000),
        call(12, tool="Edit", tool_input={"file_path": "docs/parser.md"}),
        event("turn.end", 13, content={"last_assistant_message": "Docs updated."}, metadata={}),
    ]


def test_session_bundle_holds_the_task_turns_calls_and_totals(tmp_path):
    paths, project = store(tmp_path, _session())
    draft = session.build(paths, project, **ONE)

    facts = draft.facts
    assert facts["first_prompt"] == "Fix the flaky parser test"
    assert facts["last_prompt"] == "Now update the docs"
    assert facts["last_assistant_message"] == "Docs updated."
    assert (facts["turns"], facts["tool_calls"], facts["tool_failures"]) == (2, 3, 1)
    assert (facts["requests"], facts["peak_tokens"]) == (3, 40_000)
    assert isinstance(facts["findings"], list)

    newest, oldest = draft.items
    assert (newest["item_id"], oldest["item_id"]) == ("T2", "T1")
    assert newest["prompt"] == "Now update the docs"
    assert newest["occupancy_after"] == 40_000
    failed, passed = oldest["calls"]
    assert (failed["class"], failed["outcome"]) == ("Bash: uv run pytest", "failed")
    assert "AssertionError" in failed["error"]
    assert (passed["outcome"], passed["duration_ms"]) == ("ok", 1200)
    assert oldest["assistant_final"] == "The test passes now."
    assert oldest["ended"] is True


def _run(paths, project, runner, **overrides):
    options: dict[str, Any] = {
        "alias": "repo",
        "mode": "session",
        "provider": "claude",
        "session_id": "s1",
        "since": None,
        "until": None,
        "model": "sonnet",
        "effort": None,
        "timeout": 60.0,
        "max_bundle_tokens": None,
        "language": "English",
        "dry_run": False,
        "output": None,
        "runner": runner,
    }
    return insights.run(paths, project, **(options | overrides))


def _answer(evidence):
    return {
        "summary": "Fixed the test, then moved to docs.",
        "judgement": {
            "state": "progress",
            "rationale": "The failing run was followed by a passing one.",
            "unblock": None,
            "confidence": "high",
            "evidence_ids": evidence,
        },
        "recommendations": [],
        "rule_candidates": [],
    }


def test_the_judgement_is_validated_grounded_and_rendered(tmp_path):
    paths, project = store(tmp_path, _session())
    item = session.build(paths, project, **ONE).items[1]
    cited = [item["item_id"], item["calls"][0]["evidence_id"]]
    output = tmp_path / "session.md"

    result = _run(
        paths, project, lambda request: llm.Result(_answer(cited), None, {}), output=output
    )

    assert result["status"] == "ok"
    assert result["judgement"]["state"] == "progress"
    assert result["judgement"]["ungrounded"] is False
    text = output.read_text(encoding="utf-8")
    assert "## Judgement (a second opinion, not a verdict)" in text
    assert "**progress** (confidence high)" in text
    invented = _run(paths, project, lambda request: llm.Result(_answer(["nope"]), None, {}))
    assert invented["judgement"]["ungrounded"] is True
    wrong = _answer(cited)
    wrong["judgement"]["state"] = "done"
    rejected = _run(paths, project, lambda request: llm.Result(wrong, None, {}))
    assert rejected["reason"] == "malformed_output"


def test_the_session_mode_needs_one_known_session(tmp_path):
    paths, project = store(tmp_path, _session())
    runner = lambda request: pytest.fail("no call without a session")  # noqa: E731
    with pytest.raises(StorageError, match="--session"):
        _run(paths, project, runner, session_id=None, provider=None)
    with pytest.raises(StorageError, match="Unknown session"):
        _run(paths, project, runner, session_id="missing")


def test_render_keeps_other_modes_free_of_a_judgement_section():
    text = render.markdown({"mode": "errors", "summary": "s", "recommendations": []})
    assert "Judgement" not in text


def _cli(monkeypatch, capsys, paths, *args):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-watchdog",
            "--config",
            str(paths.config),
            "--data",
            str(paths.data),
            "--runtime",
            str(paths.runtime),
            *args,
        ],
    )
    return main()


def test_cli_days_sets_the_window_and_excludes_since(tmp_path, monkeypatch, capsys):
    paths, project = store(tmp_path, _session())
    before = datetime.now(UTC)
    code = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--project",
        str(project.id),
        "--days",
        "3",
        "--dry-run",
    )
    assert code == 0
    since = datetime.fromisoformat(json.loads(capsys.readouterr().out)["window"]["since"])
    assert before - timedelta(days=3, seconds=5) <= since <= datetime.now(UTC) - timedelta(days=3)
    for bad in (["--days", "0"], ["--days", "3", "--since", BASE.isoformat()]):
        with pytest.raises(SystemExit) as exit_info:
            _cli(monkeypatch, capsys, paths, "insights", "errors", "--dry-run", *bad)
        assert exit_info.value.code == 2


def test_a_long_turn_keeps_its_start_and_its_latest_calls(tmp_path):
    events = [event("turn.start", 0, content={"prompt": "Loop"}, metadata={})]
    events += [call(1 + index, f"echo {index}") for index in range(150)]
    paths, project = store(tmp_path, events)
    draft = session.build(paths, project, **ONE)
    (turn,) = draft.items
    inputs = [row["input"] for row in turn["calls"]]
    assert len(inputs) == session.CALLS_PER_TURN
    assert inputs[0] == '{"command": "echo 0"}'
    assert inputs[-1] == '{"command": "echo 149"}'
    assert '{"command": "echo 20"}' not in inputs
    assert draft.coverage["calls_omitted_from_turns"] == 30
