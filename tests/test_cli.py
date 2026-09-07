import subprocess
import sys


def test_help_describes_bootstrap_without_starting_collection():
    result = subprocess.run(
        [sys.executable, "-m", "agent_watchdog", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "{daemon,hook,hooks,project,doctor,report,sessions}" in result.stdout


def test_unknown_command_is_rejected():
    result = subprocess.run(
        [sys.executable, "-m", "agent_watchdog", "start"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
