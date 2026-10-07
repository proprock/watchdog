"""Opt-in checks for shell installations on the current host."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform_diagnostic


@pytest.fixture
def benchmark():
    path = Path(__file__).parents[2] / "scripts" / "benchmark_hooks.py"
    spec = importlib.util.spec_from_file_location("benchmark_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bash_is_usable() -> bool:
    """Reject a WSL launcher stub masquerading as a functional Bash executable."""
    try:
        result = subprocess.run(
            ["bash", "-c", "printf ok"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == "ok"


def _available_shells():
    shells = ["direct"]
    for shell in ("bash", "cmd.exe", "powershell.exe", "pwsh.exe"):
        if not shutil.which(shell):
            continue
        if shell == "bash" and not _bash_is_usable():
            continue
        shells.append(shell)
    return shells


@pytest.mark.parametrize("shell", _available_shells())
def test_hook_invocation_round_trips_harmless_arguments(benchmark, shell):
    """Evidence class: local subprocess shell contract, not an offline unit test."""
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
    result = subprocess.run(
        benchmark.hook_invocation(shell, arguments), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\x1f") == values
