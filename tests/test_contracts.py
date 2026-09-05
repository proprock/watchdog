import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_watchdog.config import (
    Config,
    ConfigError,
    Limits,
    Overrides,
    Project,
    load_config,
    save_config,
    user_paths,
)
from agent_watchdog.events import Envelope


def test_missing_config_uses_defaults_without_creating_files(tmp_path):
    config = load_config(tmp_path / "config.toml")
    assert config.defaults.content_days == 30
    assert config.defaults.metrics_days == 180
    assert config.defaults.project_bytes == 2 * 1024**3
    assert config.projects == ()
    assert list(tmp_path.iterdir()) == []
    paths = user_paths()
    assert all(path.is_absolute() for path in (paths.config, paths.data, paths.runtime))


@pytest.mark.parametrize(
    "content",
    [
        "[broken",
        "schema_version = 2",
        "schema_version = true",
        'unknown = "secret-value"',
        '[defaults]\ncontent_days = "30"',
        "[defaults]\ncontent_days = 0",
        "[defaults]\nproject_bytes = 10",
        '[[projects]]\nid = "not-a-uuid"\nroot = "relative"',
    ],
)
def test_invalid_config_is_rejected_without_overwrite_or_content_in_error(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret-value" not in str(error.value)
    assert path.read_text() == content


def test_default_config_round_trip(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    config = Config()
    save_config(path, config)
    assert load_config(path) == config


def test_project_overrides_round_trip_and_do_not_change_other_projects(tmp_path):
    first = Project(id=uuid4(), root=tmp_path / "one", overrides=Overrides(content_days=7))
    second = Project(id=uuid4(), root=tmp_path / "two")
    config = Config(defaults=Limits(metrics_days=90), projects=(first, second))
    path = tmp_path / "config.toml"
    save_config(path, config)
    restored = load_config(path)
    assert restored == config
    assert first.overrides.apply(restored.defaults).content_days == 7
    assert second.overrides.apply(restored.defaults).content_days == 30
    assert second.overrides.apply(restored.defaults).metrics_days == 90
    with pytest.raises(ValidationError):
        Config(projects=(Project(id=uuid4(), root=tmp_path, overrides=Overrides(inbox_bytes=1)),))


def test_save_refuses_unsupported_existing_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("schema_version = 2\n")
    with pytest.raises(ConfigError):
        save_config(path, Config())
    assert path.read_text() == "schema_version = 2\n"


def test_failed_config_replace_preserves_original_and_cleans_temporary_file(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "config.toml"
    save_config(path, Config())
    original = path.read_bytes()

    def fail_replace(*args):
        raise PermissionError("Simulated file lock")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(ConfigError):
        save_config(path, Config(defaults=Limits(content_days=7)))
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_duplicate_persisted_id_is_rejected(tmp_path):
    project = Project(id=uuid4(), root=tmp_path / "one")
    with pytest.raises(ValidationError):
        Config(projects=(project, Project(id=project.id, root=tmp_path / "two")))


def test_envelope_round_trip_preserves_ids_and_explicit_unknowns():
    event = Envelope(
        provider="codex",
        project_id=uuid4(),
        kind="tool.finish",
        source="hook",
        payload={"codex": {"tool_response": "opaque", "usage": None}},
        availability={"usage": "unknown"},
    )
    restored = Envelope.model_validate_json(event.model_dump_json())
    assert restored == event
    assert restored.session_id is None
    assert restored.turn_id is None
    assert restored.occurred_at is None
    assert restored.checkout_id is None
    assert restored.surface == "unknown"
    assert restored.received_at.tzinfo is not None
    assert restored.availability["usage"] == "unknown"


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"project_id": "invalid"},
        {"session_id": 12},
        {"session_id": ""},
        {"received_at": "2026-09-05T12:00:00"},
        {"payload": {"claude": {"text": "wrong namespace"}}},
        {"unexpected": "field"},
    ],
)
def test_invalid_envelope_is_rejected(update):
    payload = {
        "provider": "codex",
        "project_id": str(uuid4()),
        "kind": "unknown",
        "source": "hook",
        **update,
    }
    with pytest.raises(ValidationError):
        Envelope.model_validate_json(json.dumps(payload))


def test_unknown_provider_event_is_preserved_without_claiming_support():
    event = Envelope(
        provider="future-provider",
        project_id=uuid4(),
        source="hook",
        kind="unknown",
        occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
        payload={"future-provider": {"hook_event_name": "FutureEvent"}},
    )
    assert event.kind == "unknown"
    assert Envelope.model_validate_json(event.model_dump_json()).payload == event.payload
