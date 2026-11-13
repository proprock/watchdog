"""Tests for the CLI skeleton."""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner
from wdi.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "config" in result.output


def test_config_init_creates_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"

    result = runner.invoke(app, ["config", "init", "--path", str(cfg_path)])

    assert result.exit_code == 0
    assert cfg_path.is_file()
    assert "machine_id" in result.output


def test_import_command_with_custom_config(tmp_path: Path) -> None:
    import json

    root = tmp_path / "claude" / "slug"
    root.mkdir(parents=True)
    session_line = {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-06-01T10:00:00Z",
        "cwd": "C:/Repos/demo",
        "message": {"role": "user", "content": "hi"},
    }
    sid = "22222222-2222-4222-8222-222222222222"
    (root / f"{sid}.jsonl").write_text(
        json.dumps(session_line) + "\n", encoding="utf-8", newline="\n"
    )

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 33333333-3333-4333-8333-333333333333",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "sources:",
                "  claude_code:",
                f"    root: {(tmp_path / 'claude').as_posix()}",
                "  codex:",
                "    enabled: false",
                f"    root: {(tmp_path / 'missing').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import", "-c", str(cfg)])

    assert result.exit_code == 0
    assert (tmp_path / "wdi.sqlite").is_file()


def test_import_command_shows_progress(tmp_path: Path) -> None:
    import json

    root = tmp_path / "claude" / "slug"
    root.mkdir(parents=True)
    sid = "22222222-2222-4222-8222-222222222222"
    (root / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-06-01T10:00:00Z",
                "cwd": "C:/Repos/demo",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 33333333-3333-4333-8333-333333333333",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "sources:",
                "  claude_code:",
                f"    root: {(tmp_path / 'claude').as_posix()}",
                "  codex:",
                "    enabled: false",
                f"    root: {(tmp_path / 'missing').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "Overall" in result.output
    assert "claude" in result.output
    assert "Session import" in result.output


def test_import_without_config_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "-c", str(tmp_path / "absent.yaml")])

    assert result.exit_code == 1


def test_init_creates_config_then_imports(tmp_path: Path, monkeypatch) -> None:
    # The default config points at the real user dirs, so stub the import to keep
    # the test from reading real sessions; we only assert the config is created.
    import wdi.ingest.service as service

    monkeypatch.setattr(service, "run_import", lambda settings, full=False, on_progress=None: {})
    cfg = tmp_path / "config.yaml"

    result = runner.invoke(app, ["init", "-c", str(cfg)])

    assert result.exit_code == 0
    assert cfg.is_file()
    assert "Config created" in result.output
    assert "machine_id" in result.output


def test_init_uses_existing_config_and_imports(tmp_path: Path) -> None:
    import json

    root = tmp_path / "claude" / "slug"
    root.mkdir(parents=True)
    sid = "44444444-4444-4444-8444-444444444444"
    (root / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-06-01T10:00:00Z",
                "cwd": "C:/Repos/demo",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 55555555-5555-4555-8555-555555555555",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "sources:",
                "  claude_code:",
                f"    root: {(tmp_path / 'claude').as_posix()}",
                "  codex:",
                "    enabled: false",
                f"    root: {(tmp_path / 'missing').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["init", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "Using existing config" in result.output
    assert "Overall" in result.output
    assert "claude" in result.output
    assert (tmp_path / "wdi.sqlite").is_file()


def test_init_force_recreates_config(tmp_path: Path, monkeypatch) -> None:
    import wdi.ingest.service as service

    monkeypatch.setattr(service, "run_import", lambda settings, full=False, on_progress=None: {})
    cfg = tmp_path / "config.yaml"
    runner.invoke(app, ["init", "-c", str(cfg)])
    first_id = cfg.read_text(encoding="utf-8")

    result = runner.invoke(app, ["init", "-c", str(cfg), "-f"])

    assert result.exit_code == 0
    assert cfg.read_text(encoding="utf-8") != first_id  # new machine_id


def _seeded_config(tmp_path: Path, *, model: str = "claude-opus-4-8") -> Path:
    """Create a config and an imported DB with one Claude session."""
    import json

    root = tmp_path / "claude" / "slug"
    root.mkdir(parents=True)
    sid = "22222222-2222-4222-8222-222222222222"
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-06-01T10:00:00Z",
            "cwd": "C:/Repos/demo",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-06-01T10:00:01Z",
            "message": {
                "role": "assistant",
                "model": model,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "content": [
                    {"type": "text", "text": "ok"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "x.py"},
                    },
                ],
            },
        },
    ]
    (root / f"{sid}.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8", newline="\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 33333333-3333-4333-8333-333333333333",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "sources:",
                "  claude_code:",
                f"    root: {(tmp_path / 'claude').as_posix()}",
                "  codex:",
                "    enabled: false",
                f"    root: {(tmp_path / 'missing').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )
    runner.invoke(app, ["import", "-c", str(cfg)])
    return cfg


def test_stats_command(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path)

    result = runner.invoke(app, ["stats", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "Overview" in result.output


def test_doctor_reports_hook_token_differences(tmp_path: Path, monkeypatch) -> None:
    from wdi.metrics import hooks

    cfg = _seeded_config(tmp_path)
    monkeypatch.setattr(
        hooks,
        "reconcile_tokens",
        lambda _conn: [
            {
                "session_id": "synthetic-session",
                "machine_id": "synthetic-machine",
                "provider": "claude",
                "scope": "main",
                "metric": "input_tokens",
                "wdi": 200,
                "watchdog": 100,
                "differs": True,
            },
            {
                "session_id": "unknown-session",
                "machine_id": "synthetic-machine",
                "provider": "claude",
                "scope": "main",
                "metric": "input_tokens",
                "wdi": 9000,
                "watchdog": None,
                "differs": None,
            },
        ],
    )

    result = runner.invoke(app, ["doctor", "-c", str(cfg)])

    assert result.exit_code == 0, result.output
    assert "Token reconciliation" in result.output
    assert "synthetic-session" in result.output
    assert "input_tokens" in result.output
    assert "200" in result.output and "100" in result.output
    assert "wdi=200 watchdog=100 unavailable_pairs=1" in result.output


def test_stats_warns_about_unpriced_models(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path, model="claude-fable-5")

    result = runner.invoke(app, ["stats", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "Unpriced models" in result.output
    assert "claude-fable-5" in result.output
    assert "cost totals are underestimated" in result.output


def _seeded_codex_config(tmp_path: Path) -> Path:
    """Create a config and an imported DB with one Codex session."""
    import json

    sid = "019eae50-c169-73c1-81ba-eeba321f3026"
    root = tmp_path / "codex" / "2026" / "06" / "09"
    root.mkdir(parents=True)
    lines = [
        {
            "timestamp": "2026-06-09T20:00:00Z",
            "type": "session_meta",
            "payload": {"id": sid, "cwd": "C:/Repos/demo", "source": "vscode"},
        },
        {
            "timestamp": "2026-06-09T20:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4"},
        },
        {
            "timestamp": "2026-06-09T20:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hi"},
        },
        {
            "timestamp": "2026-06-09T20:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 800,
                        "cached_input_tokens": 100,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 40,
                    },
                    "model_context_window": 10_000,
                },
                "rate_limits": {"primary": {"used_percent": 25.0}},
            },
        },
        {
            "timestamp": "2026-06-09T20:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "duration_ms": 4000,
                "time_to_first_token_ms": 300,
            },
        },
    ]
    (root / f"rollout-2026-06-09T20-00-00-{sid}.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8", newline="\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 33333333-3333-4333-8333-333333333333",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "sources:",
                "  claude_code:",
                "    enabled: false",
                f"    root: {(tmp_path / 'missing').as_posix()}",
                "  codex:",
                f"    root: {(tmp_path / 'codex').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )
    runner.invoke(app, ["import", "-c", str(cfg)])
    return cfg


def test_stats_codex_only_shows_codex_rows(tmp_path: Path) -> None:
    cfg = _seeded_codex_config(tmp_path)

    result = runner.invoke(app, ["stats", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "(codex only)" in result.output
    assert "Avg TTFT" in result.output
    assert "Truncated outputs" not in result.output
    assert "(claude only)" not in result.output


def test_stats_claude_only_hides_codex_rows(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path)

    result = runner.invoke(app, ["stats", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "Avg TTFT" not in result.output
    assert "Reasoning tokens" not in result.output


def test_sessions_list_command(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path)

    result = runner.invoke(app, ["sessions", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "22222222" in result.output


def test_sessions_show_by_prefix(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path)

    result = runner.invoke(app, ["sessions", "show", "22222222", "-c", str(cfg)])

    assert result.exit_code == 0
    assert "claude-opus-4-8" in result.output


def test_sessions_show_missing(tmp_path: Path) -> None:
    cfg = _seeded_config(tmp_path)

    result = runner.invoke(app, ["sessions", "show", "deadbeef", "-c", str(cfg)])

    assert result.exit_code == 1


def _minimal_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "machine_id: 66666666-6666-4666-8666-666666666666",
                f"db_path: {(tmp_path / 'wdi.sqlite').as_posix()}",
                "server:",
                "  host: 0.0.0.0",
                "  port: 9111",
            ]
        ),
        encoding="utf-8",
    )
    return cfg


def test_serve_uses_config_host_port(tmp_path: Path, monkeypatch) -> None:
    import wdi.web as web

    cfg = _minimal_config(tmp_path)
    calls: dict[str, object] = {}
    monkeypatch.setattr(web, "create_app", lambda settings: "APP")

    import uvicorn

    def fake_run(app, *, host, port, log_level):  # noqa: ANN001, ANN202
        calls.update(app=app, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = runner.invoke(app, ["serve", "-c", str(cfg)])

    assert result.exit_code == 0
    assert calls == {"app": "APP", "host": "0.0.0.0", "port": 9111}


def test_serve_flags_override_and_import_runs_first(tmp_path: Path, monkeypatch) -> None:
    import wdi.ingest.service as service
    import wdi.web as web

    cfg = _minimal_config(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(web, "create_app", lambda settings: "APP")
    monkeypatch.setattr(
        service, "run_import", lambda settings, full=False: order.append("import") or {}
    )

    import uvicorn

    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: order.append(f"serve:{kw['host']}:{kw['port']}")
    )

    result = runner.invoke(
        app, ["serve", "-c", str(cfg), "--import", "--host", "127.0.0.1", "--port", "8123"]
    )

    assert result.exit_code == 0
    assert order == ["import", "serve:127.0.0.1:8123"]


def test_mcp_command_runs_stdio_server(tmp_path: Path, monkeypatch) -> None:
    import wdi.mcp as mcp_mod

    cfg = _minimal_config(tmp_path)
    calls: list[str] = []

    class FakeServer:
        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(mcp_mod, "create_mcp_server", lambda settings: FakeServer())

    result = runner.invoke(app, ["mcp", "-c", str(cfg)])

    assert result.exit_code == 0
    assert calls == ["run"]


def test_analyze_shows_estimate_and_aborts_on_no(tmp_path: Path, monkeypatch) -> None:
    import wdi.llm.service as service

    cfg = _seeded_config(tmp_path)
    called: list[object] = []
    monkeypatch.setattr(service, "run_analyzer", lambda *a, **k: called.append(1))

    result = runner.invoke(app, ["analyze", "-c", str(cfg)], input="n\n")

    assert result.exit_code == 0
    assert "Estimated processing" in result.output
    assert "~Cost" in result.output
    assert "Aborted" in result.output
    assert called == []  # processing never started


def test_analyze_shows_progress_while_estimating(tmp_path: Path, monkeypatch) -> None:
    import wdi.llm.service as service

    cfg = _seeded_config(tmp_path)
    called: list[object] = []
    monkeypatch.setattr(service, "run_analyzer", lambda *a, **k: called.append(1))

    result = runner.invoke(app, ["analyze", "-c", str(cfg)], input="n\n")

    assert result.exit_code == 0
    assert "Estimating" in result.output
    assert "Estimated processing" in result.output
    assert called == []


def test_analyze_yes_skips_prompt_and_runs(tmp_path: Path, monkeypatch) -> None:
    import wdi.llm.service as service
    from wdi.llm.service import AnalysisStats

    cfg = _seeded_config(tmp_path)
    runs: list[str] = []

    def fake_run(  # noqa: ANN001, ANN202
        conn,
        backend,
        analyzer,
        flt,
        *,
        model,
        limit,
        data_dirs,
        force=False,
        workers=1,
        on_progress=None,
    ):
        assert data_dirs == [tmp_path / "unconfigured-watchdog"]
        runs.append(analyzer.key)
        return AnalysisStats(analyzed=1)

    monkeypatch.setattr(service, "run_analyzer", fake_run)

    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--yes"])

    assert result.exit_code == 0
    assert "Proceed with analysis?" not in result.output
    assert runs  # at least one analyzer processed


def test_analyze_uses_configured_codex_backend(tmp_path: Path, monkeypatch) -> None:
    from wdi.llm import backend

    cfg = _seeded_config(tmp_path)
    with cfg.open("a", encoding="utf-8") as stream:
        stream.write(
            "\nfeatures:\n  llm_analysis:\n    backend: codex\n"
            "    model: fixture-model\n    codex_executable: fixture-codex\n"
        )

    def run(args, **kwargs):
        assert args[:2] == ["fixture-codex", "exec"]
        assert args[args.index("--model") + 1] == "fixture-model"
        assert "--ephemeral" in args
        assert "--output-schema" not in args
        assert "hi" in kwargs["input"]
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps({"score": 95, "summary": "fixture"}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend.subprocess, "run", run)
    monkeypatch.setattr(backend.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--type", "summary", "--yes"])

    assert result.exit_code == 0, result.exception
    from wdi.db.connection import connect

    conn = connect(tmp_path / "wdi.sqlite")
    try:
        row = conn.execute("SELECT model, score, result FROM llm_analyses").fetchone()
        assert row is not None
        assert (row["model"], row["score"]) == ("fixture-model", 95)
        assert json.loads(row["result"])["summary"] == "fixture"
    finally:
        conn.close()


def test_analyze_all_failures_exit_nonzero(tmp_path: Path, monkeypatch) -> None:
    from agent_watchdog import readapi
    from agent_watchdog.insights import llm

    cfg = _seeded_config(tmp_path)
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "unused")
    monkeypatch.setattr(llm, "claude", lambda *a, **k: llm.Result(None, "timeout"))

    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--type", "summary", "--yes"])

    assert result.exit_code == 1
    assert "LLM analysis results" in result.stdout
    assert "timeout" in result.stderr


def test_analyze_prints_provider_limit_diagnostic_literally(tmp_path: Path, monkeypatch) -> None:
    from agent_watchdog import readapi
    from agent_watchdog.insights import llm

    cfg = _seeded_config(tmp_path)
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "unused")
    monkeypatch.setattr(
        llm,
        "claude",
        lambda *a, **k: llm.Result(
            None,
            "nonzero_exit",
            {"api_error_status": 429, "result": "Weekly allowance exhausted; reset [UTC]."},
        ),
    )

    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--type", "summary", "--yes"])

    assert "429" in result.stderr
    assert "Weekly allowance exhausted; reset [UTC]." in " ".join(result.stderr.split())
    assert result.exit_code == 1


def test_analyze_partial_failure_keeps_successes_and_exits_nonzero(
    tmp_path: Path, monkeypatch
) -> None:
    from wdi.db.connection import connect

    from agent_watchdog import readapi
    from agent_watchdog.insights import llm

    cfg = _seeded_config(tmp_path)
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "unused")
    calls = []

    def complete(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return llm.Result(None, "timeout")
        return llm.Result({"text": '{"score": 95, "summary": "fixture"}'}, None)

    monkeypatch.setattr(llm, "claude", complete)
    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--yes"])

    conn = connect(tmp_path / "wdi.sqlite")
    try:
        saved = conn.execute("SELECT score FROM llm_analyses").fetchall()
    finally:
        conn.close()
    assert len(calls) > 1
    assert len(saved) == len(calls) - 1
    assert all(row["score"] == 95 for row in saved)
    assert result.exit_code == 1
    assert "LLM analysis results" in result.stdout
    assert "timeout" in result.stderr


def test_analyze_force_passes_through(tmp_path: Path, monkeypatch) -> None:
    import wdi.llm.service as service
    from wdi.llm.service import AnalysisStats

    cfg = _seeded_config(tmp_path)
    seen: list[bool] = []

    def fake_estimate(  # noqa: ANN001, ANN202
        conn, analyzer, flt, *, model, limit, pricing, data_dirs, force=False, on_progress=None
    ):
        from wdi.llm.service import AnalysisEstimate

        assert data_dirs == [tmp_path / "unconfigured-watchdog"]
        return AnalysisEstimate(analyzer=analyzer.key, model=model, sessions=1, input_tokens=10)

    def fake_run(  # noqa: ANN001, ANN202
        conn,
        backend,
        analyzer,
        flt,
        *,
        model,
        limit,
        data_dirs,
        force=False,
        workers=1,
        on_progress=None,
    ):
        seen.append(force)
        return AnalysisStats(analyzed=1)

    monkeypatch.setattr(service, "estimate_analyzer", fake_estimate)
    monkeypatch.setattr(service, "run_analyzer", fake_run)

    result = runner.invoke(app, ["analyze", "-c", str(cfg), "--yes", "--force"])

    assert result.exit_code == 0
    assert seen and all(seen)  # force propagated to every analyzer run


def test_analyze_nothing_to_do(tmp_path: Path, monkeypatch) -> None:
    import wdi.llm.service as service

    cfg = _seeded_config(tmp_path)
    called: list[object] = []
    monkeypatch.setattr(service, "run_analyzer", lambda *a, **k: called.append(1))

    result = runner.invoke(app, ["analyze", "-c", str(cfg), "-p", "no-such-project"])

    assert result.exit_code == 0
    assert "Nothing to analyze" in result.output
    assert called == []


def test_config_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    runner.invoke(app, ["config", "init", "-p", str(cfg_path)])

    result = runner.invoke(app, ["config", "init", "-p", str(cfg_path)])
    assert result.exit_code == 1

    result = runner.invoke(app, ["config", "init", "-p", str(cfg_path), "-f"])
    assert result.exit_code == 0
