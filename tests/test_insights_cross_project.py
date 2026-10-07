"""Offline contracts for `insights --all-projects` (WD-132)."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from helpers.insights import invoke
from test_insights import T0, TRACEBACK_A, FakeRunner, _finish
from test_insights_modes import NONE, _prompt, _start, call
from test_storage_v6 import BASE

from agent_watchdog import insights
from agent_watchdog.config import Config, Overrides, Project, UserPaths, save_config
from agent_watchdog.insights import bundle, contract, errors, llm, permissions, scope, workflow
from agent_watchdog.storage import Store

SINCE = T0 - timedelta(days=1)
# The mode-test helpers stamp their events from BASE, earlier than T0.
EARLIER = BASE - timedelta(days=1)
SECRET = "sk-" + "b" * 32
SPAWN = "Exit code 2\nerror: Failed to spawn: `pytest`"


def workspace(
    tmp_path: Path,
    events_by_name: dict[str, list],
    *,
    disabled: tuple[str, ...] = (),
    empty: tuple[str, ...] = (),
) -> tuple[UserPaths, dict[str, Project]]:
    """Register one project per name (its alias is the directory name) in one config.

    A name in ``empty`` is registered without a database.
    """
    projects = {
        name: Project(
            id=uuid4(),
            root=tmp_path / name,
            overrides=Overrides(insights_llm_enabled=False) if name in disabled else Overrides(),
        )
        for name in (*events_by_name, *empty)
    }
    save_config(tmp_path / "config.toml", Config(projects=tuple(projects.values())))
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    for name, events in events_by_name.items():
        project = projects[name]
        with Store(paths.project_data(project.id), project.id) as writer:
            for event in events:
                writer.put(event.model_copy(update={"project_id": project.id}))
    return paths, projects


def run_all(paths, runner=None, **overrides):
    options: dict[str, Any] = {
        "mode": "errors",
        "provider": None,
        "since": SINCE,
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
    return insights.run_all(paths, **(options | overrides))


def _failing(at, traceback, *, session="s1", command="uv run pytest"):
    return _finish(
        uuid4(), at=at, failed=True, error=traceback, command=command, session_id=session
    )


def _two_projects(tmp_path, **kwargs):
    """`a` and `b` both hit TRACEBACK_A (b twice, in another session); only `a` hits SPAWN."""
    return workspace(
        tmp_path,
        {
            "a": [
                _failing(0, TRACEBACK_A),
                _failing(1, SPAWN, command="uv run pytest b"),
                _failing(2, SPAWN, command="uv run pytest b"),
                _failing(3, SPAWN, command="uv run pytest b"),
            ],
            "b": [
                _failing(10, TRACEBACK_A, session="s2"),
                _failing(11, TRACEBACK_A, session="s2"),
            ],
        },
        **kwargs,
    )


def _items_by_signature(bundle):
    return {item["signature"]["key_line"]: item for item in bundle["items"]}


def test_a_shared_error_merges_with_both_projects_and_ranks_by_project_count(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    result = run_all(paths, dry_run=True)
    bundle = result["bundle"]
    shared, single = bundle["items"]
    assert shared["signature"]["key_line"] == "ModuleNotFoundError: No module named '<s>'"
    # Seen in two projects outranks seen more often in one.
    assert (shared["projects"], shared["count"]) == ({"a": 1, "b": 2}, 3)
    assert (single["projects"], single["count"]) == ({"a": 3}, 3)
    assert {sample["project"] for sample in shared["samples"]} == {"a", "b"}
    assert bundle["facts"]["projects"] == ["a", "b"]
    assert bundle["facts"]["by_project"] == {"a": 4, "b": 2}
    assert bundle["coverage"]["projects_skipped"] == {"disabled": 0, "unreadable": 0}
    assert result["projects"]["included"] == ["a", "b"]


def test_sessions_with_the_same_id_in_two_projects_stay_distinct(tmp_path):
    paths, _projects = workspace(
        tmp_path,
        {"a": [_failing(0, TRACEBACK_A)], "b": [_failing(1, TRACEBACK_A)]},
    )
    (item,) = run_all(paths, dry_run=True)["bundle"]["items"]
    assert item["sessions"] == 2


def test_a_disabled_project_is_excluded_and_only_counted(tmp_path):
    paths, _projects = _two_projects(tmp_path, disabled=("b",))
    result = run_all(paths, dry_run=True)
    bundle = result["bundle"]
    assert bundle["facts"]["projects"] == ["a"]
    assert bundle["coverage"]["projects_skipped"] == {"disabled": 1, "unreadable": 0}
    assert all(set(item["projects"]) == {"a"} for item in bundle["items"])
    # The skipped project's name never reaches the model-bound bundle.
    assert '"b"' not in json.dumps(bundle["facts"] | bundle["coverage"])
    assert result["projects"] == {"included": ["a"], "disabled": ["b"], "unreadable": []}


def test_a_project_without_a_database_is_counted_not_fatal(tmp_path):
    paths, _projects = workspace(tmp_path, {"a": [_failing(0, TRACEBACK_A)]}, empty=("ghost",))
    result = run_all(paths, dry_run=True)
    assert result["bundle"]["coverage"]["projects_skipped"] == {"disabled": 0, "unreadable": 1}
    assert result["projects"]["unreadable"] == ["ghost"]


def test_no_eligible_project_refuses_the_model_call(tmp_path):
    paths, _projects = _two_projects(tmp_path, disabled=("a", "b"))
    runner = FakeRunner(llm.Result(output={}, reason=None, provenance={}))
    result = run_all(paths, runner)
    assert (result["status"], result["reason"]) == ("unavailable", "no_eligible_projects")
    assert runner.requests == []


def test_the_bundle_is_redacted_and_names_no_root_path(tmp_path):
    leak = _failing(5, f"Exit code 1\nbad key {SECRET}", command=f"curl -H {SECRET}")
    paths, _projects = workspace(tmp_path, {"a": [_failing(0, TRACEBACK_A)], "b": [leak]})
    text = json.dumps(run_all(paths, dry_run=True)["bundle"])
    assert SECRET not in text
    # json.dumps doubles Windows backslashes, so check the escaped and the POSIX spelling.
    assert json.dumps(str(tmp_path))[1:-1] not in text
    assert tmp_path.as_posix() not in text


def _answer(item_id, **overrides):
    recommendation = {
        "title": "Install the missing module",
        "cluster_ids": [item_id],
        "cause": "environment",
        "fix_kind": "instruction",
        "target": "claude",
        "recommendation": "Run uv sync before tests.",
        "instruction_draft": "Run uv sync first.",
        "confidence": "high",
        "evidence_ids": [item_id],
        "scope": "user",
        "target_file": "~/.claude/CLAUDE.md",
        "project": None,
    }
    return {
        "summary": "Two environment problems.",
        "recommendations": [recommendation | overrides],
        "rule_candidates": [],
    }


def _ok(answer):
    return FakeRunner(llm.Result(output=answer, reason=None, provenance={"version": "fake"}))


def test_user_scope_is_kept_for_an_item_seen_in_two_projects(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    runner = _ok(_answer("E1"))
    (recommendation,) = run_all(paths, runner)["recommendations"]
    assert recommendation["scope"] == "user"
    assert recommendation["target_file"] == "~/.claude/CLAUDE.md"
    assert recommendation["scope_notes"] == []
    assert recommendation["ungrounded"] is False
    # The prompt explains the scopes and the schema carries them.
    assert "scope" in runner.requests[0].system_prompt
    assert (
        "scope" in runner.requests[0].schema["properties"]["recommendations"]["items"]["properties"]
    )


def test_a_one_project_pattern_is_never_proposed_at_user_scope(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    # E2 is the SPAWN cluster, seen only in `a`; the fake model wrongly says "user".
    (recommendation,) = run_all(paths, _ok(_answer("E2")))["recommendations"]
    assert recommendation["scope"] == "project"
    assert recommendation["project"] == "a"
    assert recommendation["target_file"] == "CLAUDE.md"
    assert "narrowed to project scope" in recommendation["scope_notes"][0]


def test_a_user_scope_answer_citing_nothing_in_the_bundle_is_narrowed(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    (recommendation,) = run_all(paths, _ok(_answer("E99")))["recommendations"]
    assert recommendation["scope"] == "project"
    assert recommendation["project"] is None
    assert recommendation["ungrounded"] is True


def test_a_project_scope_answer_must_name_a_project_in_the_bundle(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    answer = _answer("E2", scope="project", target_file="CLAUDE.md", project="elsewhere")
    (recommendation,) = run_all(paths, _ok(answer))["recommendations"]
    assert (recommendation["scope"], recommendation["project"]) == ("project", "a")
    assert "not in the bundle" in recommendation["scope_notes"][0]


def test_a_user_target_outside_the_user_files_is_flagged(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    (recommendation,) = run_all(paths, _ok(_answer("E1", target_file="src/x.py")))[
        "recommendations"
    ]
    assert recommendation["scope"] == "user"
    assert "user-level" in recommendation["scope_notes"][0]


def test_a_cross_project_answer_without_scope_is_malformed(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    answer = _answer("E1")
    del answer["recommendations"][0]["scope"]
    result = run_all(paths, _ok(answer))
    assert (result["status"], result["reason"]) == ("unavailable", "malformed_output")


@pytest.mark.parametrize("module", [errors, permissions, workflow])
def test_cross_schemas_are_self_contained_and_carry_scope(module):
    schema = contract.json_schema(module.CrossOutput)
    assert "$ref" not in json.dumps(schema)
    item = schema["properties"]["recommendations"]["items"]
    assert item["additionalProperties"] is False
    assert {"scope", "target_file", "project"} <= set(item["properties"])


def test_the_fitted_bundle_drops_single_project_items_first(tmp_path):
    paths, _projects = _two_projects(tmp_path)
    full = run_all(paths, dry_run=True)
    shared, single = full["bundle"]["items"]
    # Room for the shared item and its frame, but not for the single-project item.
    frame = len(json.dumps(full["bundle"] | {"items": []}).encode("utf-8"))
    size = len(json.dumps(shared).encode("utf-8"))
    budget = max(1000, int((frame + size + 600) / bundle.BYTES_PER_TOKEN) + 1)
    assert frame + size + len(json.dumps(single).encode("utf-8")) > budget * bundle.BYTES_PER_TOKEN
    tight = run_all(paths, dry_run=True, max_bundle_tokens=budget)
    (kept,) = tight["bundle"]["items"]
    assert kept["projects"] == {"a": 1, "b": 2}
    assert tight["bundle"]["coverage"]["truncated"]["items"] == 1
    assert tight["estimated_bundle_tokens"] <= budget


def _chain(session, start):
    commands = ("uv run ruff check .", "uv run pytest -q", "git commit -m x")
    return [call(start + index, command, session=session) for index, command in enumerate(commands)]


def test_workflow_counts_a_chain_across_projects_that_neither_reaches_alone(tmp_path):
    # Two occurrences in one session of `a`, one in a session of `b`: the thresholds are
    # three occurrences in two sessions, so neither project reports it alone.
    a = [*_chain("s1", 0), *_chain("s1", 10)]
    b = _chain("s1", 0)
    paths, projects = workspace(tmp_path, {"a": a, "b": b})
    for project in projects.values():
        assert workflow.build(paths, project, **NONE).items == []
    bundle = run_all(paths, mode="workflow", since=EARLIER, dry_run=True)["bundle"]
    (item,) = bundle["items"]
    assert item["kind"] == "sequence"
    assert item["projects"] == {"a": 2, "b": 1}
    # Session "s1" exists in both projects and counts as two sessions.
    assert item["sessions"] == 2
    assert bundle["facts"]["calls_by_project"] == {"a": 6, "b": 3}


def test_workflow_loops_keep_their_project_and_class_spread(tmp_path):
    poll = [call(index, "gh run view 7", session="s1") for index in range(4)]
    paths, _projects = workspace(tmp_path, {"a": poll, "b": poll})
    items = run_all(paths, mode="workflow", since=EARLIER, dry_run=True)["bundle"]["items"]
    assert {item["project"] for item in items} == {"a", "b"}
    assert all(item["projects"] == {"a": 1, "b": 1} for item in items)


def test_permissions_merge_a_gated_class_across_projects(tmp_path):
    def gated(tool_use_id):
        return [
            _start(0, "Bash", {"command": "uv run pytest -q"}, tool_use_id),
            _prompt(1, "Bash"),
            call(31, "uv run pytest -q", tool_use_id=tool_use_id),
        ]

    paths, _projects = workspace(tmp_path, {"a": gated("t1"), "b": gated("t1")})
    bundle = run_all(paths, mode="permissions", since=EARLIER, dry_run=True)["bundle"]
    (item,) = bundle["items"]
    assert item["class"] == "Bash: uv run pytest"
    assert (item["prompts"], item["projects"]) == (2, {"a": 1, "b": 1})
    assert item["waited_s"]["unknown"] == 0
    assert bundle["facts"]["prompts_by_project"] == {"a": 1, "b": 1}


def test_single_project_bundles_carry_no_project_fields(tmp_path):
    paths, projects = workspace(tmp_path, {"a": [_failing(0, TRACEBACK_A)]})
    draft = errors.build(paths, projects["a"], **NONE)
    assert "projects" not in draft.items[0]
    assert "by_project" not in draft.facts
    assert "projects" not in draft.facts


def test_share_keeps_a_fair_slice_per_project_in_order():
    members = [("a", 1), ("a", 2), ("a", 3), ("b", 4), ("b", 5), ("a", 6)]
    assert scope.share(members, 4, lambda member: member[0]) == [
        ("a", 3),
        ("b", 4),
        ("b", 5),
        ("a", 6),
    ]
    assert scope.share(members[:3], 2, lambda member: member[0]) == [("a", 2), ("a", 3)]
    assert scope.share(members, 4, lambda member: member[0], newest=False) == [
        ("a", 1),
        ("a", 2),
        ("b", 4),
        ("b", 5),
    ]


def _cli(monkeypatch, capsys, paths, *args):
    try:
        code = invoke(monkeypatch, paths, *args)
    except SystemExit as error:
        return error.code, capsys.readouterr().err
    return code, json.loads(capsys.readouterr().out)


def test_cli_dry_run_over_all_projects_sends_nothing(tmp_path, monkeypatch, capsys):
    paths, _projects = _two_projects(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run must not call the runner")

    monkeypatch.setattr(llm, "claude", forbidden)
    code, result = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--all-projects",
        "--since",
        SINCE.isoformat(),
        "--dry-run",
    )
    assert code == 0
    assert result["project"] == "2 projects: a, b"
    assert result["bundle"]["facts"]["projects"] == ["a", "b"]


def test_cli_rejects_all_projects_with_a_project_or_session_or_other_mode(
    tmp_path, monkeypatch, capsys
):
    paths, projects = _two_projects(tmp_path)
    code, _message = _cli(
        monkeypatch, capsys, paths, "insights", "errors", "--all-projects", "--project", "a"
    )
    assert code == 2
    code, result = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--all-projects",
        "--provider",
        "claude",
        "--session",
        "s1",
    )
    assert code == 1
    assert "--session" in result["message"]
    code, result = _cli(monkeypatch, capsys, paths, "insights", "tokens", "--all-projects")
    assert code == 1
    assert "workflow" in result["message"]
