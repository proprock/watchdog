import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_hooks.py"
    spec = importlib.util.spec_from_file_location("benchmark_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_waits_for_live_pid_after_status_invalidation(benchmark, monkeypatch):
    ready = {"state": "running", "alive": True, "pid": 123}
    reports = iter([{}, {"state": "starting", "alive": True}, ready])
    monkeypatch.setattr(
        benchmark, "run", lambda _: SimpleNamespace(stdout=json.dumps(next(reports)))
    )
    monkeypatch.setattr(benchmark.time, "sleep", lambda _: None)
    assert benchmark.wait_for_daemon(["watchdog"]) == ready


def test_benchmark_rejects_stale_daemon_and_bounds_readiness(benchmark, monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "run",
        lambda _: SimpleNamespace(stdout='{"state":"unavailable","alive":false,"pid":123}'),
    )
    clock = iter([0, 16])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not become ready"):
        benchmark.wait_for_daemon(["watchdog"])


ARGUMENTS = ["/adapter", "--config", "/c", "hook", "claude"]


def test_hook_invocation_direct_is_the_bare_argument_list(benchmark):
    assert benchmark.hook_invocation("direct", ARGUMENTS) == ARGUMENTS


def test_hook_invocation_bash_wraps_with_a_posix_command_string(benchmark):
    wrapped = benchmark.hook_invocation("bash", ARGUMENTS)
    assert wrapped[:2] == ["bash", "-c"]
    assert wrapped[2] == "/adapter --config /c hook claude"


def test_hook_invocation_cmd_builds_a_windows_command_string(benchmark):
    wrapped = benchmark.hook_invocation("cmd.exe", ARGUMENTS)
    assert wrapped == 'cmd.exe /d /s /c ""/adapter" "--config" "/c" "hook" "claude""'


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe"])
def test_hook_invocation_powershell_builds_command_without_command_windows(benchmark, shell):
    wrapped = benchmark.hook_invocation(shell, ARGUMENTS)
    assert wrapped[0] == shell and wrapped[-2] == "-Command"
    assert wrapped[-1] == "& '/adapter' '--config' '/c' 'hook' 'claude'"


def test_benchmark_cli_accepts_claude_provider_and_bash_shell(benchmark, monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark.sys, "argv", ["benchmark_hooks.py", "--output", "x", "--samples", "10"]
    )
    with pytest.raises(SystemExit):
        benchmark.main()
    assert "at least 20 samples" in capsys.readouterr().err
    assert set(benchmark.SHELLS) == {"direct", "bash", "cmd.exe", "powershell.exe", "pwsh.exe"}
