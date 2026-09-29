"""Offline contracts for `insights context` and `insights tokens` (WD-124)."""

import json
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from test_storage_v6 import BASE, claude_usage, codex_usage

from agent_watchdog import insights
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.insights import budget, context, llm, timeline, tokens
from agent_watchdog.inspection import database, tool_finishes
from agent_watchdog.storage import Store

SESSION = "s1"


def usage(offset, request_id, occupancy, *, output=100, write=0, model="claude-sonnet-5"):
    """A Claude request whose context is `occupancy` tokens, mostly cache reads."""
    return claude_usage(
        uuid4(),
        session=SESSION,
        offset=offset,
        request_id=request_id,
        model=model,
        response={
            "input_tokens": 2,
            "cache_read_input_tokens": occupancy - 2 - write,
            "cache_creation_input_tokens": write,
            "output_tokens": output,
        },
    )


def hook(kind, offset, *, provider="claude", agent_id=None, **fields):
    return Envelope(
        provider=provider,
        project_id=uuid4(),
        session_id=SESSION,
        agent_id=agent_id,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={provider: fields},
    )


def finish(offset, command, response="ok", *, tool="Bash", agent_id=None):
    return hook(
        "tool.finish",
        offset,
        agent_id=agent_id,
        hook_event_name="PostToolUse",
        tool_name=tool,
        content={"tool_input": {"command": command}, "tool_response": response},
        metadata={},
    )


def store(tmp_path, events):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    with Store(paths.project_data(project.id), project.id) as writer:
        for event in events:
            writer.put(event.model_copy(update={"project_id": project.id}))
    return paths, project


def load(paths, project):
    with database(paths, project) as db:
        return timeline.requests(db, provider=None, session_id=None, since=None, until=None)


def test_occupancy_uses_each_providers_counters():
    claude_row = {"input_tokens": 2, "cached_input_tokens": 900, "cache_write_input_tokens": 98}
    codex_row = {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0}
    assert timeline.occupancy("claude", claude_row) == 1000
    assert timeline.occupancy("codex", codex_row) == 1000
    assert timeline.occupancy("claude", claude_row | {"cached_input_tokens": None}) is None


def test_requests_drop_reemitted_synthetic_and_spike_rows_but_keep_a_compaction(tmp_path):
    events = [
        usage(0, "r1", 10_000),
        usage(1, "r1", 10_000),  # the same response re-emitted
        usage(2, "r2", 12_000),
        usage(3, "r3", 0, model="<synthetic>"),
        usage(4, "r4", 25_000),  # one response summing two iterations
        usage(5, "r5", 13_000),
        usage(6, "r6", 20_000),
        usage(7, "r7", 4_000),  # compaction: the drop stays
    ]
    paths, project = store(tmp_path, events)
    series, counts = load(paths, project)
    sequence = series[("claude", SESSION, None)]
    assert [request.occupancy for request in sequence] == [10_000, 12_000, 13_000, 20_000, 4_000]
    assert counts["duplicate_usage_rows"] == 1
    assert counts["synthetic_rows"] == 1
    assert counts["spike_rows_excluded"] == 1


def test_steps_attribute_growth_to_the_calls_that_finished_in_between(tmp_path):
    events = [
        usage(0, "r1", 10_000, output=500),
        finish(1, "uv run pytest -q"),
        finish(2, "git diff"),
        usage(3, "r2", 16_500),
        finish(10, "late call after the last request"),
    ]
    paths, project = store(tmp_path, events)
    series, _counts = load(paths, project)
    _types, finishes = tool_finishes(
        paths, project, provider=None, session_id=None, since=None, until=None
    )
    steps, unattributed = timeline.steps(series, finishes)
    (step,) = steps
    assert step.appended == 6_000
    assert len(step.finishes) == 2
    assert len(unattributed) == 1


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Bash", {"command": "uv run pytest tests/x.py -q"}, "Bash: uv run pytest"),
        ("Bash", {"command": "cd C:/repo && git diff --stat"}, "Bash: git diff"),
        (
            "Bash",
            {"command": "PYTHONIOENCODING=utf-8 python -m pytest -x"},
            "Bash: python -m pytest",
        ),
        ("PowerShell", {"command": "Get-Content -Raw .\\TENETS.md"}, "PowerShell: get-content"),
        ("Bash", {"command": "C:\\tools\\rg.exe -n foo src"}, "Bash: rg"),
        ("Bash", {"command": ["bash", "-lc", "ls"]}, "Bash: bash"),
        ("Read", {"file_path": "a.py"}, "Read"),
        ("mcp__server__search", {"q": "x"}, "mcp__server__search"),
        ("Bash", None, "Bash: (unknown)"),
    ],
)
def test_command_class_groups_by_program_and_subcommand(tool, tool_input, expected):
    assert tokens.command_class(tool, tool_input) == expected


def test_context_profiles_growth_turns_compactions_and_resets(tmp_path):
    events = [
        hook("session.start", 0, metadata={"source": "startup"}),
        hook("turn.start", 0, content={"prompt": "Fix the parser"}, metadata={}),
        usage(1, "r1", 40_000),
        finish(2, "uv run pytest"),
        usage(3, "r2", 90_000),
        hook("compaction.start", 4, metadata={"trigger": "auto"}),
        hook("compaction.end", 5, metadata={"trigger": "auto", "compact_summary": "Kept: parser"}),
        usage(6, "r3", 20_000),
        hook("turn.start", 7, content={"prompt": "Now write the docs"}, metadata={}),
        usage(8, "r4", 30_000),
        usage(9, "r5", 5_000),  # a drop without a compaction event
        claude_usage(
            uuid4(),
            session=SESSION,
            offset=3,
            request_id="sub",
            model="claude-haiku-4-5",
            response={
                "input_tokens": 8,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 9000,
                "output_tokens": 10,
            },
        ).model_copy(update={"agent_id": "agent-1"}),
    ]
    paths, project = store(tmp_path, events)
    draft = context.build(paths, project, provider=None, session_id=None, since=None, until=None)

    (item,) = draft.items
    assert item["item_id"] == "C1"
    assert item["baseline_tokens"] == 40_000
    assert item["peak_tokens"] == 90_000
    assert item["start_sources"] == ["startup"]
    assert [turn["occupancy_after"] for turn in item["turns"]] == [40_000, 30_000]
    assert item["turns"][1]["prompt"] == "Now write the docs"
    (compaction,) = item["compactions"]
    assert compaction["trigger"] == "auto"
    assert (compaction["before_tokens"], compaction["after_tokens"]) == (90_000, 20_000)
    assert compaction["summary"] == "Kept: parser"
    assert item["resets_count"] == 1
    assert item["largest_steps"][0]["appended_tokens"] == 90_000 - 40_000 - 100
    assert item["subagents"]["count"] == 1
    assert draft.facts["by_provider"]["claude"]["compactions"] == {"auto": 1}
    assert item["peak_share_of_window"] is None
    assert draft.facts["context_windows_reported"] == {}
    assert draft.coverage["compaction_summaries"] == 1


def test_context_uses_windows_the_cli_reported(tmp_path):
    paths, project = store(tmp_path, [usage(0, "r1", 40_000), usage(1, "r2", 250_000)])
    provenance = {"context_window": 1_000_000, "models": ["claude-sonnet-5"]}
    budget.remember(paths, "sonnet", provenance, BASE)
    draft = context.build(paths, project, provider=None, session_id=None, since=None, until=None)
    assert draft.facts["context_windows_reported"] == {"claude-sonnet-5": 1_000_000}
    assert draft.items[0]["peak_share_of_window"] == 0.25


def test_tokens_ranks_calls_classes_repeats_and_cache_rebuilds(tmp_path):
    events = [
        usage(0, "r1", 10_000, output=0),
        finish(1, "uv run pytest -q", "x" * 5000),
        usage(2, "r2", 18_000, output=0),
        finish(3, "cat big.log", "y" * 900),
        usage(4, "r3", 19_000, output=0),
        finish(5, "cat big.log", "y" * 900),
        usage(900, "r4", 20_000, output=0, write=19_000),  # rewritten after an idle gap
    ]
    paths, project = store(tmp_path, events)
    draft = tokens.build(paths, project, provider=None, session_id=None, since=None, until=None)

    first = draft.items[0]
    assert first["item_id"] == "T1"
    assert first["class"] == "Bash: uv run pytest"
    assert first["attributed_tokens"] == 8_000
    assert first["response_head"].startswith("xxx")
    classes = {row["class"]: row for row in draft.facts["by_class"]}
    assert classes["Bash: cat"]["calls"] == 2
    assert classes["Bash: cat"]["attributed_tokens"] == 1_000 + 1_000
    (repeat,) = draft.facts["repeats"]
    assert repeat["count"] == 2
    assert draft.facts["cache_rebuilds"]["count"] == 1
    assert draft.facts["cache_rebuilds"]["examples"][0]["gap_seconds"] == 896
    totals = draft.facts["by_provider"]["claude"]
    assert totals["input_read_tokens"] == 10_000 + 18_000 + 19_000 + 20_000
    assert draft.coverage["tool_finishes"] == 3


def test_a_call_in_a_flat_interval_costs_zero_and_a_reset_leaves_it_unknown(tmp_path):
    events = [
        usage(0, "r1", 10_000, output=0),
        finish(1, "git status"),
        usage(2, "r2", 10_000, output=0),
        finish(3, "git log"),
        usage(4, "r3", 2_000, output=0),  # reset: the growth across it is unknown
    ]
    paths, project = store(tmp_path, events)
    draft = tokens.build(paths, project, provider=None, session_id=None, since=None, until=None)
    classes = {row["class"]: row for row in draft.facts["by_class"]}
    assert classes["Bash: git status"]["attributed_calls"] == 1
    assert classes["Bash: git status"]["attributed_tokens"] == 0
    assert classes["Bash: git log"]["attributed_calls"] == 0


def test_context_answers_are_validated_and_grounded_by_item_id(tmp_path):
    events = [usage(0, "r1", 40_000), usage(1, "r2", 60_000)]
    paths, project = store(tmp_path, events)
    answer = {
        "summary": "One long session.",
        "recommendations": [
            {
                "title": "Compact earlier",
                "item_ids": ["C1"],
                "kind": "compaction_threshold",
                "target": "claude",
                "recommendation": "Set an earlier threshold.",
                "draft": "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70",
                "expected_effect": "Peaks stay under 60K.",
                "confidence": "low",
                "evidence_ids": ["C1"],
            },
            {
                "title": "Invented",
                "item_ids": ["C9"],
                "kind": "other",
                "target": "claude",
                "recommendation": "x",
                "draft": None,
                "expected_effect": "x",
                "confidence": "low",
                "evidence_ids": ["C9"],
            },
        ],
        "rule_candidates": [],
    }
    captured: list[llm.Request] = []

    def runner(request: llm.Request) -> llm.Result:
        captured.append(request)
        return llm.Result(answer, None, {})

    result = insights.run(
        paths,
        project,
        alias="repo",
        mode="context",
        provider=None,
        session_id=None,
        since=BASE - timedelta(days=1),
        until=None,
        model="sonnet",
        effort=None,
        timeout=60.0,
        max_bundle_tokens=None,
        language="English",
        dry_run=False,
        output=None,
        runner=runner,
    )
    assert result["status"] == "ok"
    grounded, invented = result["recommendations"]
    assert grounded["ungrounded"] is False
    assert invented["ungrounded"] is True
    assert "compaction_threshold" in json.dumps(captured[0].schema)


def test_cli_dry_run_accepts_the_tokens_mode(tmp_path, monkeypatch, capsys):
    paths, project = store(tmp_path, [usage(0, "r1", 10_000), usage(1, "r2", 12_000)])
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
            "insights",
            "tokens",
            "--project",
            str(project.id),
            "--since",
            (BASE - timedelta(days=1)).isoformat(),
            "--dry-run",
        ],
    )
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["bundle"]["mode"] == "tokens"
    assert result["facts"]["by_provider"]["claude"]["requests"] == 2


def test_codex_occupancy_comes_from_input_tokens(tmp_path):
    delta = {
        "input_tokens": 30_000,
        "cached_input_tokens": 29_000,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
        "total_tokens": 30_010,
    }
    event = codex_usage(
        uuid4(), session=SESSION, offset=0, response_id="resp_1", turn_id="t1", delta=delta
    )
    paths, project = store(tmp_path, [event])
    series, _counts = load(paths, project)
    assert [request.occupancy for request in series[("codex", SESSION, None)]] == [30_000]
