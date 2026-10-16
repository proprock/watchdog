import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from helpers.insights import invoke

from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.insights import budget, llm
from agent_watchdog.storage import Store


def _calibrate_module():
    path = Path(__file__).parents[1] / "scripts" / "calibrate.py"
    spec = importlib.util.spec_from_file_location("calibrate_selection_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backend_and_model_have_independent_remembered_windows(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    observed = datetime(2026, 10, 16, tzinfo=UTC)

    budget.remember(
        paths, "sonnet", {"context_window": 200_000, "models": ["claude-sonnet"]}, observed
    )
    budget.remember(
        paths,
        "sonnet",
        {"context_window": 80_000, "models": ["codex-sonnet"]},
        observed,
        backend="codex",
    )

    assert budget.plan(paths, "sonnet", None)["context_window"] == 200_000
    assert budget.plan(paths, "sonnet", None, backend="codex")["context_window"] == 80_000
    assert budget.reported_windows(paths) == {"claude-sonnet": 200_000}
    memory = json.loads(budget.memory_path(paths).read_text(encoding="utf-8"))
    assert "sonnet" in memory["backends"]["claude"]
    assert "sonnet" in memory["backends"]["codex"]


def test_legacy_window_is_read_only_for_claude(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    path = budget.memory_path(paths)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 1, "models": {"sonnet": {"context_window": 160_000}}})
    )

    assert budget.plan(paths, "sonnet", None)["context_window"] == 160_000
    assert budget.plan(paths, "sonnet", None, backend="codex")["context_window"] is None


def test_legacy_model_name_cannot_read_a_backend_entry(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    path = budget.memory_path(paths)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "models": {},
                "backends": {"codex": {"codex:model": {"context_window": 90_000}}},
            }
        )
    )

    assert budget.plan(paths, "codex:model", None)["context_window"] is None


def test_dry_run_reports_the_selected_backend_model_and_effort(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    project.root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    with Store(paths.project_data(project.id), project.id):
        pass

    assert (
        invoke(
            monkeypatch,
            paths,
            "insights",
            "errors",
            "--project",
            str(project.id),
            "--backend",
            "codex",
            "--model",
            "custom-codex-model",
            "--effort",
            "high",
            "--dry-run",
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["analysis"] == {
        "backend": "codex",
        "model": "custom-codex-model",
        "effort": "high",
    }


def test_cli_routes_codex_selection_independently_of_observation_provider(
    tmp_path, monkeypatch, capsys
):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    project.root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    with Store(paths.project_data(project.id), project.id):
        pass
    requests = []

    def fake_runner(backend, *, forbidden_roots):
        assert backend == "codex"
        assert forbidden_roots == [project.root]

        def call(request):
            requests.append(request)
            return llm.Result(None, "isolation_unavailable", {"provider": "codex"})

        return call

    monkeypatch.setattr(llm, "runner", fake_runner)
    monkeypatch.setattr(
        llm,
        "claude",
        lambda *_args, **_kwargs: pytest.fail("Codex failure must not fall back to Claude"),
    )
    assert (
        invoke(
            monkeypatch,
            paths,
            "insights",
            "errors",
            "--project",
            str(project.id),
            "--provider",
            "codex",
            "--backend",
            "codex",
            "--model",
            "o5-custom",
            "--effort",
            "high",
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    (request,) = requests
    assert (request.backend, request.model, request.effort) == ("codex", "o5-custom", "high")
    assert output["analysis"]["backend"] == "codex"
    assert output["window"]["provider"] == "codex"


@pytest.mark.parametrize("model", [None, "", "   "])
def test_cli_requires_explicit_model_for_codex(tmp_path, monkeypatch, capsys, model):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    monkeypatch.setattr(llm, "runner", lambda *_args, **_kwargs: pytest.fail("must validate first"))

    args = [
        "insights",
        "errors",
        "--project",
        str(project.id),
        "--backend",
        "codex",
        "--dry-run",
    ]
    if model is not None:
        args.extend(("--model", model))
    assert invoke(monkeypatch, paths, *args) == 1
    assert "Select --model" in capsys.readouterr().out


def test_observed_codex_provider_keeps_claude_as_the_default_analysis_backend(
    tmp_path, monkeypatch, capsys
):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    project.root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    with Store(paths.project_data(project.id), project.id):
        pass
    requests = []

    def fake_runner(backend, *, forbidden_roots):
        assert backend == "claude"
        return lambda request: (
            requests.append(request)
            or llm.Result(None, "isolation_unavailable", {"provider": "claude"})
        )

    monkeypatch.setattr(llm, "runner", fake_runner)
    assert (
        invoke(
            monkeypatch,
            paths,
            "insights",
            "errors",
            "--project",
            str(project.id),
            "--provider",
            "codex",
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    (request,) = requests
    assert request.backend == "claude"
    assert request.model == "sonnet"
    assert output["window"]["provider"] == "codex"


def test_calibration_suggestion_passes_backend_model_effort_and_one_arg_runner(
    tmp_path, monkeypatch
):
    calibrate = _calibrate_module()
    project = Project(id=uuid4(), root=tmp_path / "repo")
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    seen = {}

    def fake_run(_paths, _project, **options):
        seen.update(options)
        return {"status": "unavailable", "reason": "isolation_unavailable"}

    monkeypatch.setattr(calibrate.insights, "run", fake_run)
    monkeypatch.setattr(calibrate, "suggestion_runner", lambda _paths: object())
    calibrate.show_suggestion(
        paths,
        project,
        {"provider": "claude", "session_id": "session"},
        backend="codex",
        model="o5-custom",
        effort="medium",
    )

    assert (seen["backend"], seen["model"], seen["effort"]) == (
        "codex",
        "o5-custom",
        "medium",
    )


def test_calibration_codex_suggestion_rejects_blank_model(tmp_path, capsys):
    calibrate = _calibrate_module()
    project = Project(id=uuid4(), root=tmp_path / "repo")
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")

    calibrate.show_suggestion(
        paths,
        project,
        {"provider": "claude", "session_id": "session"},
        backend="codex",
        model="   ",
    )

    assert "select --model" in capsys.readouterr().out
