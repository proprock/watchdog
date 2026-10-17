"""All real producers reach the same durable queue and writer."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from agent_watchdog import daemon, resources
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry
from agent_watchdog.rules.api import Decision
from agent_watchdog.spool import Spool
from agent_watchdog.storage import DrainResult, MaintenanceBudget, Store


def setup(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    config = Config(projects=(project,))
    save_config(paths.config, config)
    monkeypatch.setattr(daemon, "alive", lambda _: True)
    monkeypatch.setattr(daemon, "_capture_diff_snapshot", lambda *args, **kwargs: None)
    return paths, project, config


def test_native_dev_and_control_share_the_consumer(tmp_path, monkeypatch):
    paths, project, config = setup(tmp_path, monkeypatch)
    queue = Spool(paths.data)
    raw_id = uuid4()
    queue.publish_record(
        {
            "schema_version": 2,
            "record_type": "hook",
            "provider": "codex",
            "event_id": str(raw_id),
            "received_at": datetime.now(UTC).isoformat(),
            "cwd": str(tmp_path),
            "input": {"hook_event_name": "Stop", "session_id": "native"},
        }
    )
    dev = Envelope(
        provider="codex",
        project_id=project.id,
        kind="turn.start",
        source="hook",
        session_id="developer",
    )
    assert daemon.publish(paths, dev)
    daemon._record_control(
        paths,
        "codex",
        {"session_id": "native"},
        raw_id,
        Decision(
            action="context",
            rule="synthetic",
            rule_version="1",
            context="synthetic action",
            project_id=project.id,
        ),
    )
    documents = [json.loads(path.read_bytes()) for path in queue.directory.glob("*.json")]
    assert len(documents) == 3
    assert {item["record_type"] for item in documents} == {"hook", "envelope"}
    result = DrainResult()
    assert daemon._drain_spool(paths, config, stats=result)
    assert (result.inserted, result.duplicates) == (3, 0)
    with Store(paths.project_data(project.id), project.id) as store:
        events = store.events()
    assert {item.kind for item in events} == {"turn.start", "turn.end", "control"}
    assert all(not path.exists() for path in [paths.project_data(project.id) / "inbox"])
    assert not list(queue.directory.glob("*.json"))


def test_quota_cleanup_yield_keeps_spool_without_recording_loss(tmp_path, monkeypatch):
    paths, project, config = setup(tmp_path, monkeypatch)
    with Store(paths.project_data(project.id), project.id):
        pass
    event = Envelope(provider="codex", project_id=project.id, kind="turn.start", source="hook")
    assert daemon.publish(paths, event)
    with monkeypatch.context() as patch:
        patch.setattr(resources, "available", lambda *args: 0)
        assert not daemon._drain_spool(paths, config, maintenance_budget=MaintenanceBudget(0))
    assert len(list((paths.data / "spool").glob("*.json"))) == 1
    assert resources.losses(paths.project_data(project.id))["quota"] == 0
    assert daemon._drain_spool(paths, config)
    with Store(paths.project_data(project.id), project.id) as store:
        assert [item.event_id for item in store.events()] == [event.event_id]


def test_registered_checkout_is_resolved_once_per_queue_window(tmp_path, monkeypatch):
    paths, project, config = setup(tmp_path, monkeypatch)
    for _ in range(3):
        Spool(paths.data).publish_record(
            {
                "schema_version": 2,
                "record_type": "hook",
                "provider": "codex",
                "event_id": str(uuid4()),
                "received_at": datetime.now(UTC).isoformat(),
                "cwd": str(tmp_path),
                "input": {"hook_event_name": "Stop"},
            }
        )
    calls = []
    original = Registry.resolve

    def counted(self, path, **kwargs):
        calls.append(path)
        return original(self, path, **kwargs)

    monkeypatch.setattr(Registry, "resolve", counted)
    assert daemon._drain_spool(paths, config)
    assert calls == [tmp_path]
    with Store(paths.project_data(project.id), project.id) as store:
        assert len(store.events()) == 3
