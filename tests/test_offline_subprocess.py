"""Offline subprocess contracts for the local collection boundary."""

import json
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_watchdog.config import load_config, save_config


def invoke(home, *arguments, input_text=None):
    return subprocess.run(
        [sys.executable, "-m", "agent_watchdog", "--home", str(home), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=15,
    )


def wait_for_event(database, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if database.is_file():
            with sqlite3.connect(database) as connection:
                row = connection.execute("SELECT envelope FROM events").fetchone()
            if row is not None:
                return json.loads(row[0])
        time.sleep(0.05)
    pytest.fail("Timed out waiting for the daemon to persist the hook event")


@pytest.mark.parametrize("capture_content", [True, False], ids=("content", "metadata-only"))
def test_python_hook_daemon_and_report_use_the_isolated_sqlite_store(tmp_path, capture_content):
    """Evidence class: offline subprocess integration contract."""
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_id = f"session-{capture_content}"

    try:
        added = invoke(home, "project", "add", str(project_root))
        assert added.returncode == 0, added.stderr
        project = json.loads(added.stdout)

        config = load_config(home / "config.toml")
        save_config(
            home / "config.toml",
            config.model_copy(
                update={
                    "defaults": config.defaults.model_copy(
                        update={"capture_content": capture_content}
                    )
                }
            ),
        )

        started = invoke(home, "daemon", "start")
        assert started.returncode == 0, started.stderr
        assert json.loads(started.stdout)["alive"] is True

        payload = {
            "cwd": str(project_root),
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "turn_id": "turn-1",
            "prompt": "secret prompt",
            "tool_input": {"command": "secret command"},
            "tool_response": "secret response",
        }
        observed = invoke(home, "hook", "codex", input_text=json.dumps(payload))
        assert observed.returncode == 0, observed.stderr
        assert observed.stdout.strip() == "{}"

        project_id = str(config.projects[0].id)
        database = home / "data" / "projects" / project_id / "events.sqlite3"
        envelope = wait_for_event(database)
        assert envelope["provider"] == "codex"
        assert envelope["session_id"] == session_id
        assert envelope["kind"] == "turn.start"
        content = envelope["payload"]["codex"]["content"]
        if capture_content:
            assert content["prompt"] == "secret prompt"
        else:
            assert content == "omitted"
            assert "secret" not in json.dumps(envelope)

        report = invoke(home, "report", "--project", project["project"], "--session", session_id)
        assert report.returncode == 0, report.stderr
        assert json.loads(report.stdout)["session_ids"] == [session_id]
    finally:
        if (home / "config.toml").exists():
            paused = invoke(home, "daemon", "pause")
            assert paused.returncode == 0, paused.stderr
            assert json.loads(paused.stdout)["alive"] is False
