import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def canary():
    path = Path(__file__).parents[1] / "scripts" / "codex_hook_canary.py"
    spec = importlib.util.spec_from_file_location("codex_hook_canary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handler_writes_content_free_process_marker(canary, monkeypatch, tmp_path):
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(canary, "process_chain", lambda: ["python.exe", "cmd.exe", "codex.exe"])

    canary.handle(marker, io.StringIO('{"hook_event_name":"SessionStart","prompt":"secret"}'))

    saved = json.loads(marker.read_text(encoding="utf-8"))
    assert saved == {
        "schema_version": 1,
        "event": "SessionStart",
        "process_chain": ["python.exe", "cmd.exe", "codex.exe"],
    }
    assert "secret" not in marker.read_text(encoding="utf-8")


def test_handler_rejects_non_object_input_without_creating_marker(canary, tmp_path):
    marker = tmp_path / "marker.json"

    canary.handle(marker, io.StringIO("[]"))

    assert not marker.exists()
    assert json.loads(marker.with_suffix(".failure.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "reason": "input is not a JSON object",
    }


def test_validate_marker_requires_the_expected_event_and_process_chain(canary):
    marker = {"schema_version": 1, "event": "SessionStart", "process_chain": ["a", "b"]}

    canary.validate_marker(marker, ["a", "b"])
    with pytest.raises(RuntimeError, match="process chain changed"):
        canary.validate_marker(marker, ["a", "c"])
    with pytest.raises(RuntimeError, match="unexpected canary event"):
        canary.validate_marker({**marker, "event": "Stop"}, ["a", "b"])


def test_resolve_codex_explains_when_cli_is_not_on_path(canary, monkeypatch):
    monkeypatch.setattr(canary.shutil, "which", lambda command: None)

    with pytest.raises(RuntimeError, match="pass --codex PATH"):
        canary.resolve_codex("codex")


def test_codex_version_names_the_failing_command(canary, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "missing")

    monkeypatch.setattr(canary.subprocess, "run", missing)

    with pytest.raises(RuntimeError, match="Codex version command: codex"):
        canary.codex_version("codex")


def test_command_windows_uses_the_production_powershell_form(canary, tmp_path):
    command = canary.command_windows(
        Path(r"C:\Python\python.exe"), Path(r"C:\probe.py"), tmp_path / "m.json"
    )

    assert command.startswith("& 'C:\\Python\\python.exe' 'C:\\probe.py' 'handler'")
    assert "'--marker'" in command


def test_command_windows_cmd_selects_cmd_inside_the_powershell_runner(canary, tmp_path):
    command = canary.command_windows_cmd(
        Path(r"C:\Python\python.exe"), Path(r"C:\probe.py"), tmp_path / "m.json"
    )

    assert command.startswith('& $env:ComSpec /d /s /c \'""C:\\Python\\python.exe" "C:\\probe.py"')
    assert '"handler" "--marker" "' in command and command.endswith('""\'')


@pytest.mark.skipif(os.name != "nt", reason="commandWindows is Windows-only")
def test_production_powershell_command_executes_the_handler(canary, tmp_path):
    marker = tmp_path / "marker.json"
    command = canary.command_windows(Path(sys.executable), Path(canary.__file__), marker)

    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        input='{"hook_event_name":"SessionStart"}',
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text(encoding="utf-8"))["event"] == "SessionStart"


@pytest.mark.skipif(os.name != "nt", reason="commandWindows is Windows-only")
def test_command_windows_runs_through_powershell_and_cmd(canary, tmp_path):
    marker = tmp_path / "marker.json"
    command = canary.command_windows_cmd(Path(sys.executable), Path(canary.__file__), marker)

    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        input='{"hook_event_name":"SessionStart"}',
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"


def test_standalone_cli_guard_rejects_the_desktop_host(canary, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    with pytest.raises(RuntimeError, match="standalone PowerShell"):
        canary.require_standalone_cli()


def test_profile_configuration_selects_cmd_without_prompt_content(canary):
    configuration = canary.profile_configuration("python handler", "cmd.exe /d /s /c command")

    assert 'matcher = "startup"' in configuration
    assert 'commandWindows = "cmd.exe /d /s /c command"' in configuration
    assert "prompt" not in configuration


def test_stop_owned_process_tree_closes_its_job_before_waiting(canary):

    class Process:
        alive = True

        def poll(self):
            return None if self.alive else 1

        def kill(self):
            self.alive = False

        def communicate(self, timeout):
            return "", ""

    process = Process()

    class Job:
        closed = False

        def close(self):
            self.closed = True

    job = Job()

    assert canary.stop_owned_process_tree(process, job) == ("", "")

    assert job.closed
    assert not process.alive
