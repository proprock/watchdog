"""Behavioral health evidence, independent of provider accounts and active data."""

import json
import os
from datetime import UTC, datetime

import pytest

from agent_watchdog import daemon
from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Inbox, Store, writer_lock


@pytest.fixture
def paths(tmp_path):
    return UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")


def heartbeat(paths, now, **fields):
    paths.runtime.mkdir(parents=True, exist_ok=True)
    (paths.runtime / "status.json").write_text(
        json.dumps({"heartbeat": now, "errors": [], **fields}), encoding="utf-8"
    )


@pytest.mark.parametrize("attempts, reason", [(1, "startup_failed"), (3, "startup_crash_loop")])
def test_startup_death_has_a_reason_in_one_status_call(paths, monkeypatch, attempts, reason):
    def crash(*args):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(daemon, "_poll", crash)
    for _ in range(attempts):
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            daemon.run(paths)
    report = daemon.status(paths)
    assert report["state"] == "degraded"
    assert report["alive"] is False
    assert reason in report["reasons"]
    assert report["health"]["startup"]["starts_without_heartbeat"] == attempts


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({}, "heartbeat_missing"),
        ({"heartbeat": 1}, "heartbeat_stale"),
        ({"heartbeat": "invalid"}, "heartbeat_invalid"),
    ],
)
def test_held_lock_without_a_current_heartbeat_names_the_cause(paths, payload, reason):
    paths.data.mkdir(parents=True)
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(json.dumps(payload))
    with writer_lock(paths.data / "daemon.lock"):
        report = daemon.status(paths)
    assert report["state"] == "degraded"
    assert reason in report["reasons"]


def test_idle_daemon_with_empty_queues_stays_running(paths, monkeypatch):
    def end_after_first_heartbeat(delay):
        report = daemon.status(paths)
        assert report["state"] == "running"
        assert report["reasons"] == []
        assert report["queues"]["spool"]["files"] == 0
        daemon.set_desired(paths, paused=True)

    monkeypatch.setattr(daemon.time, "sleep", end_after_first_heartbeat)
    assert daemon.run(paths) == 0
    assert daemon.status(paths)["state"] == "paused"


def test_backlog_needs_multiple_polls_and_clears_on_progress():
    tracker = daemon._BacklogTracker()
    queues = {"spool": {"files": 3}, "projects": {"project": {"files": 2}}}
    first = tracker.observe(queues, set(), now=100)
    assert first["spool"]["polls"] == 1
    later = tracker.observe(queues, set(), now=161)
    assert later["spool"]["since"] == 100
    assert later["project"]["polls"] == 2
    moved = tracker.observe(queues, {"spool"}, now=162)
    assert moved["spool"]["since"] == 162
    assert moved["project"]["since"] == 100
    queues["projects"]["project"]["files"] = 1
    assert tracker.observe(queues, set(), now=163)["project"]["since"] == 163
    queues["spool"]["files"] = 0
    assert "spool" not in tracker.observe(queues, set(), now=164)


def test_status_names_a_stalled_spool_with_a_fresh_heartbeat(paths, monkeypatch):
    monkeypatch.setattr(daemon.time, "time", lambda: 200)
    spool = paths.data / "spool"
    spool.mkdir(parents=True)
    (spool / "record.json").write_text("{}")
    heartbeat(paths, 200, backlog={"spool": {"files": 1, "since": 100, "polls": 2}})
    with writer_lock(paths.data / "daemon.lock"):
        report = daemon.status(paths)
    assert report["state"] == "degraded"
    assert "backlog_stalled" in report["reasons"]
    assert report["health"]["stalled_queues"] == ["spool"]


def test_status_detects_storage_lag_but_allows_new_traffic_after_idle(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.time, "time", lambda: 1000)
    root = tmp_path / "project"
    root.mkdir()
    daemon.mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                project_id=project.id,
                provider="codex",
                kind="unknown",
                source="hook",
                received_at=datetime.fromtimestamp(100, UTC),
            )
        )
    spool = paths.data / "spool"
    spool.mkdir()
    record = spool / "record.json"
    record.write_text("{}")
    os.utime(record, (1000, 1000))
    heartbeat(paths, 1000)
    with writer_lock(paths.data / "daemon.lock"):
        assert daemon.status(paths)["state"] == "running"
        os.utime(record, (800, 800))
        report = daemon.status(paths)
    assert report["state"] == "degraded"
    assert "storage_lag" in report["reasons"]
    assert report["health"]["storage"]["newest_stored_at"] == 100
    assert report["health"]["storage"]["newest_spool_at"] == 800


def test_an_unreadable_store_is_unknown_and_not_created(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    daemon.mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    data = paths.project_data(project.id)
    data.mkdir(parents=True)
    database = data / "events.sqlite3"
    database.write_bytes(b"invalid sqlite")
    report = daemon.status(paths)
    assert report["health"]["storage"]["unavailable"] is True
    assert database.read_bytes() == b"invalid sqlite"
    database.unlink()
    daemon.status(paths)
    assert not database.exists()


def test_project_backlog_does_not_get_hidden_by_spool_progress(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.time, "time", lambda: 200)
    root = tmp_path / "project"
    root.mkdir()
    daemon.mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    Inbox(paths.project_data(project.id)).publish(
        Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    )
    heartbeat(paths, 200, backlog={str(project.id): {"files": 1, "since": 100, "polls": 2}})
    with writer_lock(paths.data / "daemon.lock"):
        report = daemon.status(paths)
    assert "backlog_stalled" in report["reasons"]
    assert report["health"]["stalled_queues"] == [str(project.id)]


def test_poll_publishes_stall_evidence_without_repeated_status_calls(paths, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon, "_drain_spool", lambda *args, **kwargs: False)
    spool = paths.data / "spool"
    spool.mkdir(parents=True)
    (spool / "record.json").write_text("{}")

    def after_poll(delay):
        if clock[0] == 100:
            clock[0] = 161
            return
        report = daemon.status(paths)
        assert report["state"] == "degraded"
        assert "backlog_stalled" in report["reasons"]
        assert report["backlog"]["spool"]["polls"] == 2
        daemon.set_desired(paths, paused=True)

    monkeypatch.setattr(daemon.time, "sleep", after_poll)
    assert daemon.run(paths) == 0


@pytest.mark.parametrize("backlog", [None, [], {"spool": {}}, {"spool": {"since": "bad"}}])
def test_invalid_backlog_evidence_does_not_crash_status(paths, backlog):
    import time

    paths.data.mkdir(parents=True)
    heartbeat(paths, time.time(), backlog=backlog)
    with writer_lock(paths.data / "daemon.lock"):
        report = daemon.status(paths)
    assert report["state"] == "degraded"
    assert "queue_or_configuration_unavailable" in report["reasons"]
