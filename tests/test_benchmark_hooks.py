import importlib.util
import json
import shutil
import subprocess
import sys
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


def _bash_is_usable() -> bool:
    """Reject a WSL launcher stub masquerading as `bash` with no distro installed.

    Windows resolves a bare `"bash"` argv[0] the same way `hook_invocation` launches
    it: it checks System32 (where the legacy WSL bash.exe stub lives) before it ever
    consults PATH, so `shutil.which("bash")` can report a working Git Bash while the
    actual invocation still hits the stub. Probe with the same bare command the real
    test uses, not the resolved path, and require it to actually run something (the
    stub still exits 0 for an empty `-c` body).
    """
    try:
        result = subprocess.run(
            ["bash", "-c", "printf ok"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == "ok"


def _available_shells():
    shells = ["direct"]
    for shell in SHELLS:
        if not shutil.which(shell):
            continue
        if shell == "bash" and not _bash_is_usable():
            continue
        shells.append(shell)
    return shells


SHELLS = ("bash", "cmd.exe", "powershell.exe", "pwsh.exe")


@pytest.mark.parametrize("shell", _available_shells())
def test_hook_invocation_round_trips_harmless_arguments(benchmark, shell):
    """Evidence class: offline subprocess shell contract."""
    values = ["plain", "two words", "dollar$", "ampersand&", "apostrophe'"]
    if shell == "bash":
        arguments = ["printf", "%s\\x1f", *values]
    else:
        arguments = [
            sys.executable,
            "-c",
            "import sys; print('\\x1f'.join(sys.argv[1:]))",
            *values,
        ]
    invocation = benchmark.hook_invocation(shell, arguments)
    result = subprocess.run(
        invocation,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\x1f") == values
