import io
import json
from uuid import uuid4

import pytest

from agent_watchdog.config import Config, Limits, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.hooks import observe
from agent_watchdog.privacy import text
from agent_watchdog.storage import Inbox, Store


@pytest.mark.parametrize(
    "value",
    [
        'password="private-value"',
        '{"password": "private-value"}',
        "Authorization: Basic private-value",
        "AWS_SECRET_ACCESS_KEY=private-value",
        "-----BEGIN PRIVATE KEY-----\nprivate-value",
    ],
)
def test_known_secret_forms_and_idempotence(value):
    sanitized = text(value)
    assert "private-value" not in sanitized
    assert text(sanitized) == sanitized


def test_secrets_are_removed_before_every_persistent_boundary(tmp_path):
    secret = "sk-proj-" + "a" * 40
    event = Envelope(
        provider="codex",
        project_id=uuid4(),
        kind="tool.finish",
        source="hook",
        payload={"codex": {"content": {"prompt": secret, "password": "private-value"}}},
    )
    path = Inbox(tmp_path).publish(event)
    assert secret.encode() not in path.read_bytes()
    assert b"private-value" not in path.read_bytes()
    with Store(tmp_path, event.project_id) as store:
        store.put(event, artifacts={"stdout": f"Authorization: Bearer {secret}".encode()})
        assert secret.encode() not in store.read_artifact(event.event_id, "stdout")
    assert all(secret.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_malformed_input_never_enters_quarantine_as_raw_text(tmp_path):
    inbox = Inbox(tmp_path)
    inbox.directory.mkdir()
    (inbox.directory / "bad.json").write_bytes(b"password=private-value invalid JSON")
    with Store(tmp_path, uuid4()) as store:
        assert inbox.drain(store).quarantined == 1
    assert b"private-value" not in next((tmp_path / "quarantine").glob("*.bad")).read_bytes()


def test_content_capture_defaults_on_and_can_be_disabled(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    events = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: events.append(event)
    )
    raw = json.dumps(
        {
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "explain this code; password=private-value",
        }
    ).encode()
    save_config(paths.config, Config(projects=(project,)))
    observe(paths, io.BytesIO(raw))
    assert "explain this code" in events[-1].model_dump_json()
    assert "private-value" not in events[-1].model_dump_json()
    save_config(paths.config, Config(defaults=Limits(capture_content=False), projects=(project,)))
    observe(paths, io.BytesIO(raw))
    assert "explain this code" not in events[-1].model_dump_json()


def test_hook_log_records_only_safe_decision_metadata(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    emitted = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: emitted.append(event) or True
    )
    secret = "sk-proj-" + "a" * 40
    session_id = "provider-session-secret"
    raw = json.dumps(
        {
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "prompt": secret,
            "tool_response": "private tool output",
        }
    ).encode()
    save_config(paths.config, Config(defaults=Limits(log_level="DEBUG"), projects=(project,)))

    observe(paths, io.BytesIO(raw))

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=hook event=observe decision=admitted" in body
    assert str(project.id) in body and str(emitted[0].event_id) in body
    for forbidden in (secret, session_id, str(tmp_path), "private tool output", "prompt"):
        assert forbidden not in body
