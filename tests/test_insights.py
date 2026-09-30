"""Offline contracts for `insights`: evidence bundles, grounding, and the isolated runner.

No test here spawns a provider CLI: the pipeline takes its runner as a callable, and
the `claude -p` runner is exercised against a recorded fake of `_proc.run`.
"""

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_watchdog import insights
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Limits, Overrides, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.insights import budget, bundle, contract, errors, llm, render
from agent_watchdog.storage import Store

T0 = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _finish(
    project_id,
    *,
    at: int,
    tool_name: str = "Bash",
    failed: bool = False,
    error: str | None = None,
    command: str = "uv run pytest",
    session_id: str = "s1",
    agent_id: str | None = None,
    provider: str = "claude",
) -> Envelope:
    content: dict = {"tool_input": {"command": command}}
    if not failed:
        content["tool_response"] = {"stdout": "ok", "stderr": ""}
    return Envelope(
        provider=provider,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        kind="tool.finish",
        source="hook",
        received_at=T0 + timedelta(seconds=at),
        payload={
            provider: {
                "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
                "tool_name": tool_name,
                "content": content,
                "metadata": {"error": error} if error is not None else {},
            }
        },
    )


def _store(tmp_path, events, *, defaults: Limits | None = None, overrides=None):
    project = Project(id=uuid4(), root=tmp_path / "repo", overrides=overrides or Overrides())
    config = Config(projects=(project,), defaults=defaults or Limits())
    save_config(tmp_path / "config.toml", config)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    with Store(paths.project_data(project.id), project.id) as store:
        for event in events:
            store.put(event.model_copy(update={"project_id": project.id}))
    return paths, project


TRACEBACK_A = (
    "Exit code 1\nTraceback (most recent call last):\n"
    '  File "C:\\repo\\x.py", line 12, in <module>\n'
    "ModuleNotFoundError: No module named 'foo'"
)
TRACEBACK_B = (
    "Exit code 1\nTraceback (most recent call last):\n"
    '  File "/home/u/y.py", line 99, in <module>\n'
    "ModuleNotFoundError: No module named 'bar'"
)


def _sample_events(project_id):
    return [
        _finish(project_id, at=0, failed=True, error=TRACEBACK_A, command="uv run pytest a"),
        _finish(project_id, at=1, command="uv sync && uv run pytest a"),
        _finish(project_id, at=2, failed=True, error=TRACEBACK_B, command="uv run pytest b"),
        _finish(
            project_id,
            at=3,
            tool_name="Read",
            failed=True,
            error="File does not exist.",
            command="",
        ),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TRACEBACK_A, "ModuleNotFoundError: No module named '<s>'"),
        (
            "Exit code 127\n/usr/bin/bash: line 1: Get-ChildItem: command not found",
            "<path>: line <n>: Get-ChildItem: command not found",
        ),
        (
            "Exit code 2\nUsing CPython 3.12.13\nerror: Failed to spawn: `pytest`",
            "error: Failed to spawn: `pytest`",
        ),
        ("File does not exist.", "File does not exist."),
        ("", ""),
    ],
)
def test_error_key_line_keeps_the_failure_and_drops_volatile_parts(text, expected):
    assert errors.key_line(text) == expected


def test_errors_bundle_clusters_failures_and_links_the_recovering_call(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    draft = errors.build(paths, project, provider=None, session_id=None, since=None, until=None)

    assert draft.facts["tool_finishes"] == 4
    assert draft.facts["failures"] == 3
    # Claude signals success with PostToolUse; its responses carry no exit code.
    assert draft.facts["successes"] == 1
    assert draft.coverage["unclassified_outcome"] == 0
    first, second = draft.items
    assert first["cluster_id"] == "E1"
    assert first["count"] == 2
    assert first["signature"]["tool_name"] == "Bash"
    assert first["signature"]["exit_code"] == 1
    assert first["recovered"] == 1
    recovery = first["recoveries"][0]
    assert recovery["evidence_id"] == str(events[1].event_id)
    assert recovery["failed_evidence_id"] == str(events[0].event_id)
    assert "uv sync" in recovery["tool_input"]
    assert second["cluster_id"] == "E2"
    assert second["signature"]["tool_name"] == "Read"
    assert second["recovered"] == 0
    assert set(first["event_ids"]) == {str(events[0].event_id), str(events[2].event_id)}


def test_a_failing_captured_response_is_never_also_counted_as_a_success(tmp_path):
    event = _finish(uuid4(), at=0).model_copy(
        update={
            "payload": {
                "claude": {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "content": {"tool_input": {}, "tool_response": {"exit_code": 2}},
                    "metadata": {},
                }
            }
        }
    )
    paths, project = _store(tmp_path, [event])
    draft = errors.build(paths, project, provider=None, session_id=None, since=None, until=None)
    assert draft.facts["failures"] == 1
    assert draft.facts["successes"] == 0


def test_errors_bundle_reports_coverage_instead_of_zero(tmp_path):
    stripped = Envelope(
        provider="claude",
        project_id=uuid4(),
        session_id="s1",
        kind="tool.finish",
        source="hook",
        received_at=T0,
        availability={"content": "unavailable"},
    )
    paths, project = _store(tmp_path, [stripped])
    draft = errors.build(paths, project, provider=None, session_id=None, since=None, until=None)
    assert draft.items == []
    assert draft.coverage["content_expired"] == 1


def test_errors_bundle_applies_the_credential_filter(tmp_path):
    secret = "sk-" + "a" * 32
    event = _finish(
        uuid4(),
        at=0,
        failed=True,
        error=f"Exit code 1\nbad key {secret}",
        command=f"curl -H {secret}",
    )
    paths, project = _store(tmp_path, [event])
    draft = errors.build(paths, project, provider=None, session_id=None, since=None, until=None)
    assert secret not in json.dumps(draft.items)


def test_excerpt_keeps_head_and_tail_of_oversized_text():
    text = "A" * 30_000 + "MIDDLE" + "Z" * 30_000
    clipped = bundle.excerpt(text, limit=1000)
    assert clipped is not None
    assert clipped.startswith("A")
    assert clipped.endswith("Z")
    assert "MIDDLE" not in clipped
    assert "bytes omitted" in clipped
    assert len(clipped.encode()) < 1200
    assert bundle.excerpt("short", limit=1000) == "short"
    assert bundle.excerpt(None) is None


def test_fit_keeps_ranked_items_within_budget_and_reports_truncation():
    items = [{"cluster_id": f"E{index}", "blob": "x" * 2200} for index in range(1, 11)]
    draft = bundle.Draft(facts={"failures": 10}, coverage={}, items=items)
    fitted = bundle.fit(draft, mode="errors", window={}, max_tokens=3000)
    kept = [item["cluster_id"] for item in fitted["items"]]
    assert kept == [f"E{index}" for index in range(1, len(kept) + 1)]
    assert 0 < len(kept) < 10
    assert fitted["coverage"]["truncated"]["items"] == 10 - len(kept)
    assert fitted["coverage"]["truncated"]["bytes"] > 0
    assert bundle.estimate_tokens(json.dumps(fitted)) <= 3000
    untouched = bundle.fit(draft, mode="errors", window={}, max_tokens=1_000_000)
    assert untouched["coverage"]["truncated"] == {"items": 0, "bytes": 0}


def _answer(events, **overrides):
    recommendation = {
        "title": "Install the missing module",
        "cluster_ids": ["E1"],
        "cause": "environment",
        "fix_kind": "environment",
        "target": "claude",
        "recommendation": "Run uv sync before tests.",
        "instruction_draft": None,
        "confidence": "high",
        "evidence_ids": [str(events[0].event_id)],
    }
    answer = {
        "summary": "One environment problem.",
        "recommendations": [recommendation],
        "rule_candidates": [],
    }
    return answer | overrides


class FakeRunner:
    def __init__(self, result: llm.Result):
        self.result = result
        self.requests: list[llm.Request] = []

    def __call__(self, request: llm.Request) -> llm.Result:
        self.requests.append(request)
        return self.result


def _run(paths, project, runner, **overrides):
    options: dict[str, Any] = {
        "alias": "repo",
        "mode": "errors",
        "provider": None,
        "session_id": None,
        "since": T0 - timedelta(days=1),
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


def test_run_grounds_each_recommendation_in_the_bundle(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    invented = _answer(events)["recommendations"][0] | {
        "title": "Invented",
        "evidence_ids": ["not-in-bundle"],
    }
    answer = _answer(events)
    answer["recommendations"].append(invented)
    answer["rule_candidates"] = [
        {
            "name": "powershell_cmdlet_in_bash",
            "kind": "deterministic",
            "condition_sketch": "tool_name = 'Bash' and error matches 'Get-ChildItem'",
            "action": "log",
            "rationale": "Shell mismatch is mechanically detectable.",
            "evidence_ids": [],
        }
    ]
    runner = FakeRunner(llm.Result(output=answer, reason=None, provenance={"version": "fake"}))

    result = _run(paths, project, runner)

    assert result["status"] == "ok"
    grounded, ungrounded = result["recommendations"]
    assert grounded["ungrounded"] is False
    assert ungrounded["ungrounded"] is True
    assert ungrounded["unknown_evidence_ids"] == ["not-in-bundle"]
    assert result["rule_candidates"][0]["ungrounded"] is True
    assert result["provenance"]["version"] == "fake"
    assert len(result["provenance"]["bundle_sha256"]) == 64
    request = runner.requests[0]
    assert request.model == "sonnet"
    assert "untrusted" in request.system_prompt
    assert str(events[0].event_id) in request.prompt
    assert request.schema["type"] == "object"


@pytest.mark.parametrize("reason", ["timeout", "not_found", "isolation_unavailable"])
def test_run_reports_a_runner_failure_as_unavailable(tmp_path, reason):
    paths, project = _store(tmp_path, _sample_events(uuid4()))
    runner = FakeRunner(llm.Result(output=None, reason=reason, provenance={}))
    result = _run(paths, project, runner)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["recommendations"] == []


def test_run_rejects_output_outside_the_schema(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    broken = _answer(events)
    broken["recommendations"][0]["cause"] = "cosmic rays"
    runner = FakeRunner(llm.Result(output=broken, reason=None, provenance={}))
    result = _run(paths, project, runner)
    assert result["status"] == "unavailable"
    assert result["reason"] == "malformed_output"


@pytest.mark.parametrize(
    "switch",
    [
        {"defaults": Limits(insights_llm_enabled=False)},
        {"overrides": Overrides(insights_llm_enabled=False)},
    ],
    ids=("global", "project"),
)
def test_kill_switch_refuses_the_llm_call_but_not_a_dry_run(tmp_path, switch):
    paths, project = _store(tmp_path, _sample_events(uuid4()), **switch)
    runner = FakeRunner(llm.Result(output=None, reason="timeout", provenance={}))
    refused = _run(paths, project, runner)
    assert refused["status"] == "unavailable"
    assert refused["reason"] == "disabled"
    assert runner.requests == []
    dry = _run(paths, project, runner, dry_run=True)
    assert dry["status"] == "dry_run"
    assert dry["bundle"]["items"][0]["cluster_id"] == "E1"
    assert runner.requests == []


def _window_runner(events, window=1_000_000, max_output=64_000):
    return FakeRunner(
        llm.Result(
            output=_answer(events),
            reason=None,
            provenance={"context_window": window, "max_output_tokens": max_output},
        )
    )


def test_budget_scales_with_the_window_the_model_reported_last_time(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    first = _run(paths, project, _window_runner(events))
    assert first["budget"] == {
        "max_bundle_tokens": insights.DEFAULT_MAX_BUNDLE_TOKENS,
        "source": "default",
        "context_window": None,
    }
    second = _run(paths, project, _window_runner(events), dry_run=True)
    assert second["budget"]["source"] == "remembered_window"
    assert second["budget"]["context_window"] == 1_000_000
    # 80% of the window, and never past the window minus the answer and prompt reserve.
    assert second["budget"]["max_bundle_tokens"] == 800_000
    small = _run(paths, project, _window_runner(events, 200_000, 64_000), model="opus")
    assert small["budget"]["source"] == "default"
    after = _run(paths, project, _window_runner(events), model="opus", dry_run=True)
    assert after["budget"]["max_bundle_tokens"] == 200_000 - 64_000 - budget.PROMPT_RESERVE


def test_an_explicit_budget_wins_and_a_broken_memory_is_ignored(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    _run(paths, project, _window_runner(events))
    explicit = _run(paths, project, _window_runner(events), max_bundle_tokens=5000, dry_run=True)
    assert explicit["budget"] == {
        "max_bundle_tokens": 5000,
        "source": "explicit",
        "context_window": None,
    }
    budget.memory_path(paths).write_text("{not json", encoding="utf-8")
    fallback = _run(paths, project, _window_runner(events), dry_run=True)
    assert fallback["budget"]["source"] == "default"


def test_a_dry_run_never_writes_the_window_memory(tmp_path):
    paths, project = _store(tmp_path, _sample_events(uuid4()))
    _run(paths, project, FakeRunner(llm.Result(None, "timeout", {})), dry_run=True)
    assert not budget.memory_path(paths).exists()


def test_output_schema_is_self_contained_and_keeps_field_names():
    schema = contract.json_schema(errors.Output)
    assert "$ref" not in json.dumps(schema)
    assert "$defs" not in schema
    item = schema["properties"]["recommendations"]["items"]
    assert "title" in item["properties"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) >= {"title", "cause", "evidence_ids"}
    candidate = schema["properties"]["rule_candidates"]["items"]
    assert candidate["properties"]["action"]["const"] == "log"


def test_markdown_report_is_written_once_and_never_overwritten(tmp_path):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)
    runner = FakeRunner(llm.Result(output=_answer(events), reason=None, provenance={}))
    output = tmp_path / "report.md"
    result = _run(paths, project, runner, output=output)
    text = output.read_text(encoding="utf-8")
    assert result["output"] == str(output.resolve())
    assert "# Watchdog insights: errors" in text
    assert "Install the missing module" in text
    assert str(events[0].event_id) in text
    with pytest.raises(Exception, match="already exists"):
        _run(paths, project, runner, output=output)
    assert len(runner.requests) == 1


def test_markdown_keeps_a_draft_that_contains_its_own_code_fence():
    draft = "Wrapper:\n```powershell\npytest -q\n```"
    text = render.markdown(
        {
            "mode": "tokens",
            "recommendations": [
                {"title": "t", "recommendation": "r", "draft": draft, "evidence_ids": ["e"]}
            ],
        }
    )
    assert "````text\n" + draft + "\n````" in text


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
    code = main()
    return code, json.loads(capsys.readouterr().out)


def test_cli_dry_run_prints_the_bundle_without_any_llm_call(tmp_path, monkeypatch, capsys):
    events = _sample_events(uuid4())
    paths, project = _store(tmp_path, events)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run must not call the runner")

    monkeypatch.setattr(llm, "claude", forbidden)
    code, result = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--project",
        str(project.id),
        "--since",
        (T0 - timedelta(days=1)).isoformat(),
        "--dry-run",
    )
    assert code == 0
    assert result["status"] == "dry_run"
    assert result["project"] == "repo"
    assert result["bundle"]["mode"] == "errors"
    assert result["estimated_bundle_tokens"] > 0


def test_cli_returns_exit_code_one_when_the_llm_is_unavailable(tmp_path, monkeypatch, capsys):
    paths, project = _store(tmp_path, _sample_events(uuid4()))
    monkeypatch.setattr(
        llm,
        "claude",
        lambda request, **kwargs: llm.Result(output=None, reason="not_found", provenance={}),
    )
    code, result = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--project",
        str(project.id),
        "--since",
        (T0 - timedelta(days=1)).isoformat(),
    )
    assert code == 1
    assert result["reason"] == "not_found"


def test_cli_requires_provider_with_session(tmp_path, monkeypatch, capsys):
    paths, project = _store(tmp_path, [])
    code, result = _cli(
        monkeypatch,
        capsys,
        paths,
        "insights",
        "errors",
        "--project",
        str(project.id),
        "--session",
        "s1",
        "--dry-run",
    )
    assert code == 1
    assert "--provider" in result["message"]


ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "",
    "structured_output": {"summary": "ok", "recommendations": [], "rule_candidates": []},
    "total_cost_usd": 0.01,
    "duration_ms": 1200,
    "num_turns": 2,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 900, "output_tokens": 50},
    "modelUsage": {
        "claude-sonnet-5": {
            "contextWindow": 1_000_000,
            "maxOutputTokens": 64_000,
            "costBasis": "list",
            "inputTokens": 10,
        }
    },
}


class RecordedProc:
    """Stands in for `_proc.run`; answers `--version`, `--help`, then the analysis call."""

    def __init__(self, *, help_text="--safe-mode  Start with all customizations", main=None):
        self.help_text = help_text
        self.main = main or (lambda argv, kwargs: _completed(argv, json.dumps(ENVELOPE)))
        self.calls: list[tuple[list[str], dict]] = []
        self.cwd_existed = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[1:] == ["--version"]:
            return _completed(argv, "2.1.283 (Claude Code)\n")
        if argv[1:] == ["--help"]:
            return _completed(argv, self.help_text)
        self.cwd_existed = Path(kwargs["cwd"]).is_dir()
        return self.main(argv, kwargs)


def _completed(argv, stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


REQUEST = llm.Request(
    system_prompt="system rules",
    prompt="bundle text",
    schema={"type": "object"},
    model="sonnet",
    effort="high",
    timeout=30.0,
)


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "C:/bin/claude.exe")

    def install(proc: RecordedProc) -> RecordedProc:
        monkeypatch.setattr(llm._proc, "run", proc)
        return proc

    return install


def test_claude_runner_isolates_the_call_and_records_provenance(fake_claude, tmp_path):
    proc = fake_claude(RecordedProc())
    result = llm.claude(REQUEST, forbidden_roots=[tmp_path / "repo"])

    assert result.reason is None
    assert result.output == ENVELOPE["structured_output"]
    argv, kwargs = proc.calls[-1]
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert kwargs["input"] == "bundle text"
    assert "bundle text" not in argv
    assert kwargs["timeout"] == 30.0
    assert proc.cwd_existed is True
    assert not Path(kwargs["cwd"]).exists()
    provenance = result.provenance
    assert provenance["version"] == "2.1.283 (Claude Code)"
    assert provenance["models"] == ["claude-sonnet-5"]
    assert provenance["context_window"] == 1_000_000
    assert provenance["max_output_tokens"] == 64_000
    assert provenance["cost_usd"] == 0.01
    assert "system rules" not in json.dumps(provenance["argv"])


def test_claude_runner_refuses_without_safe_mode(fake_claude):
    proc = fake_claude(RecordedProc(help_text="--print only"))
    result = llm.claude(REQUEST, forbidden_roots=[])
    assert result.reason == "isolation_unavailable"
    assert [call[0][1:] for call in proc.calls] == [["--version"], ["--help"]]


def test_claude_runner_refuses_a_scratch_directory_inside_a_registered_root(fake_claude):
    proc = fake_claude(RecordedProc())
    result = llm.claude(REQUEST, forbidden_roots=[Path(tempfile.gettempdir())])
    assert result.reason == "isolation_unavailable"
    assert len(proc.calls) == 2


def test_claude_runner_reports_a_missing_binary(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.claude(REQUEST, forbidden_roots=[]).reason == "not_found"


def _timeout(argv, kwargs):
    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])


@pytest.mark.parametrize(
    ("main", "reason"),
    [
        (_timeout, "timeout"),
        (lambda argv, kwargs: _completed(argv, "", returncode=1, stderr="auth"), "nonzero_exit"),
        (lambda argv, kwargs: _completed(argv, "not json"), "malformed_output"),
        (
            lambda argv, kwargs: _completed(
                argv, json.dumps(ENVELOPE | {"is_error": True, "subtype": "error_max_turns"})
            ),
            "is_error",
        ),
        (
            lambda argv, kwargs: _completed(
                argv, json.dumps(ENVELOPE | {"structured_output": None, "result": "prose only"})
            ),
            "malformed_output",
        ),
    ],
)
def test_claude_runner_maps_failures_without_retrying(fake_claude, main, reason):
    proc = fake_claude(RecordedProc(main=main))
    result = llm.claude(REQUEST, forbidden_roots=[])
    assert result.reason == reason
    assert result.output is None
    assert len(proc.calls) == 3


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            json.dumps(
                {
                    "is_error": True,
                    "subtype": "error_during_execution",
                    "api_error_status": 429,
                    "result": "Usage limit reached",
                }
            ),
            {
                "subtype": "error_during_execution",
                "api_error_status": 429,
                "result": "Usage limit reached",
            },
        ),
        ("plain failure text", {"stdout": "plain failure text"}),
        ("", {}),
    ],
)
def test_claude_runner_keeps_the_reason_a_failed_call_printed(fake_claude, stdout, expected):
    fake_claude(
        RecordedProc(main=lambda argv, kwargs: _completed(argv, stdout, returncode=1, stderr=""))
    )
    result = llm.claude(REQUEST, forbidden_roots=[])
    assert result.reason == "nonzero_exit"
    assert {key: result.provenance[key] for key in expected} == expected
    assert result.provenance["exit_code"] == 1


def test_claude_runner_accepts_json_result_text_when_structured_output_is_absent(fake_claude):
    answer = {"summary": "ok", "recommendations": [], "rule_candidates": []}
    fake_claude(
        RecordedProc(
            main=lambda argv, kwargs: _completed(
                argv,
                json.dumps(ENVELOPE | {"structured_output": None, "result": json.dumps(answer)}),
            )
        )
    )
    assert llm.claude(REQUEST, forbidden_roots=[]).output == answer
