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


def test_stop_is_the_unsubscribed_zero_path_and_tool_events_carry_a_tool_call(benchmark):
    stop = benchmark.hook_payload("Stop", "/p", "s", 3)
    post = benchmark.hook_payload("PostToolUse", "/p", "s", 3)
    pre = benchmark.hook_payload("PreToolUse", "/p", "s", 4)

    assert stop == {"cwd": "/p", "session_id": "s", "hook_event_name": "Stop"}
    assert post["tool_name"] == "Bash" and post["tool_response"]["exit_code"] == 0
    assert pre["tool_input"] == {"command": "echo benchmark 4"} and "tool_response" not in pre


def test_each_tool_call_differs_so_the_repeat_rule_cannot_fire(benchmark):
    calls = {
        json.dumps(benchmark.hook_payload("PostToolUse", "/p", "s", index), sort_keys=True)
        for index in range(20)
    }

    assert len(calls) == 20


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


def test_warm_up_runs_sequential_then_four_way_calls(benchmark):
    import threading
    import time

    lock = threading.Lock()
    state = {"calls": 0, "active": 0, "peak": 0}

    def hook(_index: int) -> float:
        with lock:
            state["calls"] += 1
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return 0.0

    assert benchmark.warm_up(hook, 6) == 12
    assert state["calls"] == 12
    assert 1 < state["peak"] <= 4


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ((100, 300, 100), (200, 400, 200), 50.0),
        ((0, 0, 0), (100, 100, 0), 0.0),
        ((0, 100, 100), (0, 200, 200), 100.0),
    ],
)
def test_cpu_busy_percent_excludes_idle_inside_kernel_time(benchmark, before, after, expected):
    # Windows kernel time includes idle time; the tuples are (idle, kernel, user).
    assert benchmark.cpu_busy_percent(before, after) == pytest.approx(expected)


def test_cpu_busy_percent_needs_elapsed_time(benchmark):
    with pytest.raises(ValueError, match="no elapsed"):
        benchmark.cpu_busy_percent((1, 2, 3), (1, 2, 3))


def test_settle_host_waits_until_cpu_drops_below_the_limit(benchmark):
    readings = iter([93.0, 39.0, 12.0])
    assert benchmark.settle_host(30.0, 1.0, 60.0, sample=lambda _: next(readings)) == 12.0


def test_settle_host_gives_up_after_the_timeout(benchmark):
    clock = iter([0.0, 10.0, 70.0])
    with pytest.raises(RuntimeError, match="stayed busy"):
        benchmark.settle_host(30.0, 1.0, 60.0, sample=lambda _: 90.0, clock=lambda: next(clock))


@pytest.mark.parametrize(("reading", "limit"), [(None, 30.0), (90.0, None)])
def test_settle_host_does_not_wait_without_a_reading_or_limit(benchmark, reading, limit):
    assert benchmark.settle_host(limit, 1.0, 60.0, sample=lambda _: reading) == reading


def test_benchmark_cli_accepts_the_warmup_and_host_settle_options(benchmark, monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark.sys,
        "argv",
        ["benchmark_hooks.py", "--output", "x", "--samples", "10"]
        + ["--warmup", "5", "--cpu-seconds", "2", "--max-host-cpu", "30", "--settle-timeout", "9"],
    )
    with pytest.raises(SystemExit):
        benchmark.main()
    assert "at least 20 samples" in capsys.readouterr().err
