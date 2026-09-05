"""One local polling process; durable desired state and no PID-based signals."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from agent_watchdog import resources
from agent_watchdog.config import ConfigError, UserPaths, load_config, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry
from agent_watchdog.storage import (
    Inbox,
    QuotaExceeded,
    StorageError,
    Store,
    WriterBusy,
    atomic_write,
    writer_lock,
)


@contextmanager
def control_lock(paths: UserPaths, timeout: float = 5) -> Iterator[None]:
    paths.data.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        lock = writer_lock(paths.data / "control.lock")
        try:
            lock.__enter__()
            break
        except WriterBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def read_json(path: Path) -> dict | None:
    try:
        with path.open("rb") as stream:
            content = stream.read(8193)
        if len(content) > 8192:
            raise ValueError("Oversized daemon state")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Invalid daemon state")
        return result
    except FileNotFoundError:
        return None


def desired(paths: UserPaths) -> dict:
    result = read_json(paths.data / "control.json")
    if result is None:
        return {}
    if (
        result.get("schema_version") != 1
        or type(result.get("paused")) is not bool
        or not isinstance(result.get("request_id"), str)
    ):
        raise ValueError("Invalid daemon control state; file left unchanged")
    return result


def alive(paths: UserPaths) -> bool:
    if not paths.data.exists():
        return False
    try:
        with writer_lock(paths.data / "daemon.lock"):
            return False
    except WriterBusy:
        return True


def status(paths: UserPaths) -> dict:
    control = desired(paths)
    running = alive(paths)
    try:
        report = (read_json(paths.runtime / "status.json") or {}) if running else {}
        healthy = 0 <= time.time() - float(report.get("heartbeat", 0)) < 10
    except (ValueError, OSError, TypeError):
        report, healthy = {}, False
    state = "unavailable"
    if running:
        state = "running" if healthy and not report.get("errors") else "degraded"
    if control.get("paused", False):
        state = "paused"
    try:
        loss_report = {"adapter": resources.losses(paths.data)}
        for project in load_config(paths.config).projects[:32]:
            loss_report[str(project.id)] = resources.losses(paths.project_data(project.id))
    except (OSError, ValueError):
        loss_report = {"unavailable": True}
        if running and state != "paused":
            state = "degraded"
    return report | {
        "state": state,
        "alive": running,
        "request_id": control.get("request_id"),
        "losses": loss_report,
    }


def set_desired(paths: UserPaths, *, paused: bool) -> str:
    desired(paths)
    request_id = str(uuid4())
    atomic_write(
        paths.data / "control.json",
        json.dumps(
            {
                "schema_version": 1,
                "paused": paused,
                "request_id": request_id,
            }
        ).encode(),
    )
    return request_id


def launch(paths: UserPaths) -> None:
    atomic_write(paths.runtime / "status.json", b"{}")
    command = [
        sys.executable,
        "-m",
        "agent_watchdog",
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        "daemon",
        "run",
    ]
    # A stable cwd avoids pinning a checkout or a caller's temporary directory.
    process = subprocess.Popen(
        command,
        cwd=paths.data,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
        creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        if os.name == "nt"
        else 0,
    )
    process.poll()


def start(paths: UserPaths, *, explicit: bool = True) -> bool:
    with control_lock(paths, timeout=5 if explicit else 0.1):
        if explicit:
            set_desired(paths, paused=False)
        elif desired(paths).get("paused", False):
            return False
        if not alive(paths):
            launch(paths)
    return True


def stop(paths: UserPaths) -> str:
    with control_lock(paths):
        return set_desired(paths, paused=True)


def mutate_registry(paths: UserPaths, mutation: Callable[[Registry], object]) -> None:
    with control_lock(paths):
        registry = Registry(load_config(paths.config))
        mutation(registry)
        save_config(paths.config, registry.config)


def enqueue(paths: UserPaths, event: Envelope) -> bool:
    """Adapter entry point; pause and allowlist admission share the control lock."""
    with control_lock(paths, timeout=0.1):
        if desired(paths).get("paused", False):
            return False
        config = load_config(paths.config)
        project = next((item for item in config.projects if item.id == event.project_id), None)
        if project is None:
            return False
        limits = project.overrides.apply(config.defaults)
        try:
            Inbox(paths.project_data(project.id), limits=limits).publish(event)
        except QuotaExceeded:
            if not alive(paths):
                launch(paths)
            raise
        if not alive(paths):
            launch(paths)
        return True


def run(paths: UserPaths) -> int:
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as owner:
            owner.enter_context(writer_lock(paths.data / "daemon.lock"))
            _poll(paths, owner)
    except WriterBusy:
        return 0
    return 0


def _poll(paths: UserPaths, owner: ExitStack) -> None:
    instance_id = str(uuid4())
    delay = 0.25
    maintenance: dict[str, float] = {}
    while True:
        control = desired(paths)
        if control.get("paused", False):
            # Release ownership under the same lock as start, avoiding a lost restart.
            with control_lock(paths):
                if desired(paths).get("paused", False):
                    owner.close()
                    return
            continue
        errors = []
        activity = False
        try:
            config = load_config(paths.config)
            for project in config.projects:
                try:
                    root = paths.project_data(project.id)
                    limits = project.overrides.apply(config.defaults)
                    with Store(root, project.id, limits=limits) as store:
                        if time.monotonic() >= maintenance.get(str(project.id), 0):
                            store.maintain()
                            maintenance[str(project.id)] = time.monotonic() + 60
                        result = Inbox(root, limits=limits).drain(store)
                        if resources.available(root, limits) < 65536 and len(errors) < 32:
                            errors.append(str(project.id))
                    activity |= bool(
                        result.inserted + result.duplicates + result.quarantined + result.discarded
                    )
                except (OSError, StorageError, sqlite3.Error):
                    if len(errors) < 32:
                        errors.append(str(project.id))
        except ConfigError:
            errors.append("configuration")
        atomic_write(
            paths.runtime / "status.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": instance_id,
                    "pid": os.getpid(),
                    "heartbeat": time.time(),
                    "acknowledged_request": control.get("request_id"),
                    "errors": errors,
                }
            ).encode(),
        )
        delay = 0.25 if activity else min(2, delay * 2)
        time.sleep(delay)
