import json
import subprocess
import sys


def test_hooks_install_without_an_adapter_is_refused(tmp_path):
    """WD-110: production installation never silently selects the Python
    fallback; a real `hooks install` invocation with no adapter fails clearly."""
    home = tmp_path / "isolated-home"
    target = tmp_path / "hooks.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_watchdog",
            "--home",
            str(home),
            "hooks",
            "install",
            "codex",
            "--file",
            str(target),
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"error": "ValueError"}
    assert not target.exists()


def test_help_does_not_initialize_an_isolated_home(tmp_path):
    """Evidence class: offline subprocess contract."""
    home = tmp_path / "isolated-home"
    result = subprocess.run(
        [sys.executable, "-m", "agent_watchdog", "--home", str(home), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Local watchdog core" in result.stdout
    assert "--home" in result.stdout
    assert not home.exists()


def test_unknown_command_is_rejected():
    result = subprocess.run(
        [sys.executable, "-m", "agent_watchdog", "start"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
