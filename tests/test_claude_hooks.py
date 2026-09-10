import io
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import Config, Limits, Project, UserPaths, load_config, save_config
from agent_watchdog.hooks import observe
from agent_watchdog.storage import Store

FIXTURE = json.loads((Path(__file__).parent / "fixtures/hooks/claude.json").read_text())
KINDS = {
    "SessionStart": "session.start",
    "SessionEnd": "session.end",
    "UserPromptSubmit": "turn.start",
    "Stop": "turn.end",
    "PreToolUse": "tool.start",
    "PostToolUse": "tool.finish",
    "PostToolUseFailure": "tool.finish",
    "PreCompact": "compaction.start",
    "PostCompact": "compaction.end",
    "SubagentStart": "agent.start",
    "SubagentStop": "agent.end",
    "Notification": "waiting",
}


@pytest.fixture
def make(tmp_path, monkeypatch):
    def build(capture_content: bool = True):
        paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
        project = Project(id=uuid4(), root=tmp_path)
        save_config(
            paths.config,
            Config(defaults=Limits(capture_content=capture_content), projects=(project,)),
        )
        events: list = []
        monkeypatch.setattr(
            "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: events.append(event)
        )
        return paths, events

    return build


@pytest.fixture
def setup(make):
    return make()


def run(paths, payload, provider="claude"):
    observe(paths, io.BytesIO(json.dumps(payload).encode()), provider)


@pytest.mark.parametrize("sample", FIXTURE, ids=lambda sample: sample["hook_event_name"])
def test_native_events_map_to_expected_kind(setup, sample):
    """Evidence class: offline behavioral mapping contract."""
    assert {item["hook_event_name"] for item in FIXTURE} == set(KINDS)
    paths, events = setup
    run(paths, sample | {"cwd": str(paths.config.parent)})
    assert len(events) == 1
    event = events[0]
    assert event.provider == "claude"
    assert event.kind == KINDS[sample["hook_event_name"]]
    assert event.payload["claude"]["hook_event_name"] == sample["hook_event_name"]
    assert event.turn_id is None


def test_claude_prompt_id_is_promoted_to_the_envelope_turn_id(setup):
    paths, events = setup
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "prompt_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
        },
    )
    event = events[0]
    assert event.turn_id == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert event.payload["claude"]["metadata"]["prompt_id"] == event.turn_id


def test_unknown_native_name_is_preserved_not_fabricated(setup):
    paths, events = setup
    run(paths, {"cwd": str(paths.config.parent), "hook_event_name": "Frobnicate"})
    assert events[0].kind == "unknown"
    assert events[0].payload["claude"]["hook_event_name"] == "unknown"


def test_post_tool_use_failure_is_a_plain_tool_finish(setup):
    paths, events = setup
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "call-1",
            "error": "Exit code 1",
        },
    )
    event = events[0]
    assert event.kind == "tool.finish"
    assert event.availability["tool_outcome"] == "unknown"
    assert event.payload["claude"]["hook_event_name"] == "PostToolUseFailure"
    assert "error" not in event.payload["claude"]


@pytest.mark.parametrize(
    "scenario",
    ["success", "unregistered", "malformed", "oversized", "paused", "storage_failure"],
)
def test_claude_hook_emits_no_stdout(make, monkeypatch, capsys, tmp_path, scenario):
    paths, _ = make()
    raw = json.dumps({"cwd": str(paths.config.parent), "hook_event_name": "SessionStart"}).encode()
    if scenario == "unregistered":
        outside = tmp_path / "outside"
        outside.mkdir()
        raw = json.dumps({"cwd": str(outside), "hook_event_name": "Stop"}).encode()
    elif scenario == "malformed":
        raw = b"{"
    elif scenario == "oversized":
        raw = b"x" * (1024**2 + 1)
    elif scenario == "paused":
        from agent_watchdog.daemon import stop

        stop(paths)
    elif scenario == "storage_failure":

        def boom(*args, **kwargs):
            raise RuntimeError("simulated storage failure")

        monkeypatch.setattr("agent_watchdog.hooks.daemon.enqueue", boom)
    monkeypatch.setattr(
        sys, "argv", ["agent-watchdog", "--home", str(paths.config.parent), "hook", "claude"]
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert main() == 0
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""


def test_hook_retains_raw_credentials_until_export(make):
    paths, events = make(capture_content=True)
    api_key = "sk-proj-" + "a" * 40
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUse",
            "prompt": 'password="private-value"',
            "tool_response": {"stdout": f"leaked {api_key}"},
        },
    )
    blob = events[0].model_dump_json()
    assert "private-value" in blob
    assert api_key in blob


@pytest.mark.parametrize("capture", [True, False])
def test_failure_telemetry_stays_in_metadata_regardless_of_capture(make, capture):
    paths, events = make(capture_content=capture)
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "call-1",
            "error": "Exit code 1\nboom",
            "is_interrupt": False,
            "duration_ms": 12,
        },
    )
    metadata = events[0].payload["claude"]["metadata"]
    assert metadata["error"] == "Exit code 1\nboom"
    assert metadata["is_interrupt"] is False
    assert metadata["duration_ms"] == 12


def test_error_text_is_retained_until_export(make):
    paths, events = make(capture_content=True)
    api_key = "sk-proj-" + "b" * 40
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUseFailure",
            "error": f"command failed with {api_key}",
        },
    )
    assert api_key in events[0].model_dump_json()


def test_claude_transcript_paths_are_promoted_from_the_payload(make, tmp_path):
    paths, events = make()
    session_transcript = tmp_path / "session.jsonl"
    agent_transcript = tmp_path / "agent.jsonl"
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-9",
            "transcript_path": str(session_transcript),
            "agent_transcript_path": str(agent_transcript),
        },
    )
    payload = events[0].payload["claude"]
    assert payload["transcript_path"] == str(session_transcript)
    assert payload["agent_transcript_path"] == str(agent_transcript)


def test_capture_content_false_omits_the_four_content_fields(make):
    paths, events = make(capture_content=False)
    run(
        paths,
        {
            "cwd": str(paths.config.parent),
            "hook_event_name": "PostToolUse",
            "prompt": "p",
            "tool_input": {"command": "c"},
            "tool_response": "r",
            "last_assistant_message": "m",
        },
    )
    event = events[0]
    assert event.payload["claude"]["content"] == "omitted"
    assert event.availability["content"] == "unavailable"


def test_claude_and_codex_envelopes_stay_distinct_in_the_store(make, monkeypatch, tmp_path):
    paths, _ = make()
    project_id = load_config(paths.config).projects[0].id
    data = tmp_path / "data" / "projects" / str(project_id)
    with Store(data, project_id) as store:
        monkeypatch.setattr(
            "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: store.put(event)
        )
        for provider in ("codex", "claude"):
            run(paths, {"cwd": str(paths.config.parent), "hook_event_name": "Stop"}, provider)
        providers = sorted(event.provider for event in store.events())
    assert providers == ["claude", "codex"]
