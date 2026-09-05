import io
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.hooks import observe


@pytest.fixture
def setup(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    save_config(paths.config, Config(projects=(project,)))
    events = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: events.append(event)
    )
    return paths, events


@pytest.mark.parametrize(
    "native,kind",
    [
        ("PreToolUse", "tool.start"),
        ("PostToolUse", "tool.finish"),
        ("Stop", "turn.end"),
        ("SubagentStop", "agent.end"),
        ("FutureEvent", "unknown"),
    ],
)
def test_metadata_observation_without_content(setup, native, kind):
    paths, events = setup
    payload = {
        "cwd": str(paths.config.parent),
        "session_id": "s",
        "turn_id": "t",
        "hook_event_name": native,
        "tool_use_id": "call",
        "tool_response": "secret output",
        "prompt": "secret prompt",
    }
    observe(paths, io.BytesIO(json.dumps(payload).encode()))
    assert len(events) == 1
    event = events[0]
    assert event.kind == kind
    assert event.surface == "unknown"
    assert event.session_id == "s" and event.turn_id == "t"
    assert event.native_event_id is None
    assert "secret" not in event.model_dump_json()
    assert event.payload["codex"]["tool_use_id"] == "call"


@pytest.mark.parametrize(
    "raw",
    [b"{", b"[]", b"null", b"x" * (1024**2 + 1), b'{"cwd": "."}'],
    ids=["invalid", "array", "null", "oversized", "relative"],
)
def test_invalid_input_is_ignored(setup, raw):
    paths, events = setup
    observe(paths, io.BytesIO(raw))
    assert events == []


def test_unregistered_project_is_ignored(setup, tmp_path):
    paths, events = setup
    save_config(paths.config, Config())
    observe(
        paths, io.BytesIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"}).encode())
    )
    assert events == []


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        PermissionError(),
        RuntimeError(),
        subprocess.TimeoutExpired("git", 0.25),
    ],
)
def test_cli_fails_open_with_exact_noop(setup, monkeypatch, capsys, error):
    paths, events = setup

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("agent_watchdog.hooks.daemon.enqueue", fail)
    monkeypatch.setattr(
        sys, "argv", ["agent-watchdog", "--home", str(paths.config.parent), "hook", "codex"]
    )
    raw = json.dumps({"cwd": str(paths.config.parent), "hook_event_name": "Stop"}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert main() == 0
    output = capsys.readouterr()
    assert output.out == "{}\n" and output.err == ""


def test_pause_does_not_read_or_enqueue(setup):
    from agent_watchdog.daemon import stop

    paths, events = setup
    stop(paths)

    class Unreadable(io.BytesIO):
        def read(self, size=-1, /):
            pytest.fail("Paused hook read stdin")

    observe(paths, Unreadable())
    assert not events


@pytest.mark.parametrize(
    "sample",
    json.loads((Path(__file__).parent / "fixtures/hooks/codex.json").read_text()),
    ids=lambda sample: sample["hook_event_name"],
)
def test_observed_schema_variants(setup, sample):
    paths, events = setup
    observe(paths, io.BytesIO(json.dumps(sample | {"cwd": str(paths.config.parent)}).encode()))
    assert len(events) == 1
    event = events[0]
    assert event.session_id == "fixture-session"
    assert "fixture-command" not in event.model_dump_json()
    assert "fixture output" not in event.model_dump_json()
    if "tool_response" in sample:
        assert (
            event.payload["codex"]["tool_response_type"] == type(sample["tool_response"]).__name__
        )
        assert event.availability["tool_outcome"] == "unknown"
