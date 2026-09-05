import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.daemon import enqueue, mutate_registry, start, status, stop
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store, WriterBusy, writer_lock


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Timed out waiting for daemon")


@pytest.fixture
def paths(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    yield paths
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])


def test_concurrent_start_pause_and_explicit_resume(paths):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: start(paths), range(4)))
    wait_for(lambda: status(paths)["state"] == "running")
    instance = status(paths)["instance_id"]
    start(paths)
    assert status(paths)["instance_id"] == instance
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    assert status(paths)["state"] == "paused"
    assert not start(paths, explicit=False)
    assert not enqueue(
        paths, Envelope(project_id=uuid4(), provider="codex", kind="unknown", source="hook")
    )
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    assert status(paths)["instance_id"] != instance


def test_registry_mutations_do_not_lose_updates(paths, tmp_path):
    roots = [tmp_path / str(index) for index in range(4)]
    for root in roots:
        root.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda root: mutate_registry(paths, lambda registry: registry.add(root)), roots
            )
        )
    assert len(load_config(paths.config).projects) == 4


def test_daemon_drains_only_registered_events(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    event = Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    assert not enqueue(paths, event.model_copy(update={"project_id": uuid4()}))
    assert enqueue(paths, event)
    inbox = paths.project_data(project.id) / "inbox"
    wait_for(lambda: not list(inbox.glob("*.json")))
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    with Store(paths.project_data(project.id), project.id) as store:
        assert [item.event_id for item in store.events()] == [event.event_id]


def test_stale_pid_is_not_treated_as_ownership(paths):
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(
        json.dumps({"pid": 1, "heartbeat": time.time(), "instance_id": "stale"})
    )
    assert status(paths)["state"] == "unavailable"
    stop(paths)
    assert status(paths)["state"] == "paused"


def test_crash_releases_lock_and_hook_recovers(paths):
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("daemon_crash_probe.py")),
            str(paths.config.parent),
        ]
    )
    try:
        wait_for(lambda: status(paths)["state"] == "running")
        instance = status(paths)["instance_id"]
        (paths.config.parent / "crash").touch()
        assert process.wait(timeout=10) == 91
        assert status(paths)["state"] == "unavailable"
        assert start(paths, explicit=False)
        wait_for(lambda: status(paths)["state"] == "running")
        assert status(paths)["instance_id"] != instance
    finally:
        if process.poll() is None:
            (paths.config.parent / "crash").touch()
            process.wait(timeout=10)


def test_stop_followed_immediately_by_start_does_not_lose_restart(paths):
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    for _ in range(4):
        stop(paths)
        start(paths)
        wait_for(lambda: status(paths).get("acknowledged_request") == status(paths)["request_id"])
        assert status(paths)["alive"]


def test_bad_project_is_degraded_and_recovers(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    data = paths.project_data(project.id)
    data.mkdir(parents=True)
    database = data / "events.sqlite3"
    database.write_bytes(b"not a database")
    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    assert status(paths)["state"] == "degraded"
    database.unlink()
    wait_for(lambda: status(paths)["state"] == "running")


@pytest.mark.parametrize("content", ['{"schema_version": 99}', "{}"])
def test_corrupt_control_is_never_overwritten(paths, content):
    paths.data.mkdir(parents=True)
    control = paths.data / "control.json"
    control.write_text(content)
    with pytest.raises(ValueError):
        start(paths)
    with pytest.raises(ValueError):
        stop(paths)
    assert control.read_text() == content
    control.unlink()


def test_stale_heartbeat_under_a_held_lock_is_degraded(paths):
    paths.data.mkdir(parents=True)
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(json.dumps({"heartbeat": 1, "pid": 1}))
    with writer_lock(paths.data / "daemon.lock"):
        assert status(paths)["state"] == "degraded"
        assert status(paths)["alive"]
    assert status(paths)["state"] == "unavailable"


def test_hook_start_contention_is_bounded(paths):
    paths.data.mkdir(parents=True)
    with writer_lock(paths.data / "control.lock"):
        before = time.monotonic()
        with pytest.raises(WriterBusy):
            start(paths, explicit=False)
        assert time.monotonic() - before < 1


def test_independent_cli_startup_contenders_and_pause(paths):
    command = [sys.executable, "-m", "agent_watchdog", "--home", str(paths.config.parent), "daemon"]
    processes = [
        subprocess.Popen(
            command + ["start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    try:
        reports = []
        for process in processes:
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, (output, errors)
            reports.append(json.loads(output))
        assert len({report["instance_id"] for report in reports}) == 1
        result = subprocess.run(command + ["pause"], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0
        assert json.loads(result.stdout)["alive"] is False
    finally:
        stop(paths)
        for process in processes:
            process.communicate(timeout=15)
