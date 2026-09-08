"""One local polling process; durable desired state and no PID-based signals."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_watchdog import resources
from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.config import Config, ConfigError, Limits, UserPaths, load_config, save_config
from agent_watchdog.diagnostics import Level, emit, error_code
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry, Resolution
from agent_watchdog.storage import (
    Inbox,
    QuotaExceeded,
    RejectedEvent,
    StorageError,
    Store,
    WriterBusy,
    atomic_write,
    writer_lock,
)

SPOOL_BYTES = 64 * 1024**2
SPOOL_FILES = 4096
CONTROL_REQUEST_BYTES = 8192


def _log(
    paths: UserPaths,
    config: Config | None,
    level: Level,
    *,
    event: str,
    decision: str | None = None,
    reason: str | None = None,
    error_type: str | None = None,
    project_id: UUID | None = None,
    event_id: UUID | None = None,
    count: int | None = None,
    bytes: int | None = None,
) -> None:
    if config is None:
        try:
            limits = load_config(paths.config).defaults
        except ConfigError:
            limits = Limits()
    else:
        limits = config.defaults
    emit(
        paths.data,
        limits,
        level,
        component="daemon",
        event=event,
        decision=decision,
        reason=reason,
        error_type=error_type,
        project_id=project_id,
        event_id=event_id,
        count=count,
        bytes=bytes,
    )


def _capture_diff_snapshot(
    event: Envelope, checkout: Path, project_id: UUID, paths: UserPaths
) -> None:
    """Run the bounded Git read in the core, never synchronously in a hook."""
    if event.checkout_id is None:
        return
    from agent_watchdog.analysis import git_diff_fingerprint

    now = datetime.now(UTC)
    try:
        with Store(paths.project_data(project_id), project_id) as store:
            if not store.diff_due(event.checkout_id, now=now):
                return
            fingerprint = git_diff_fingerprint(checkout)
            if fingerprint is not None:
                store.record_diff_snapshot(event.checkout_id, *fingerprint, observed_at=now)
    except (OSError, StorageError, ValueError):
        return


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


def spool_payload_bytes(config: Config) -> int:
    """Largest per-project inbox payload limit; the adapter caps stdin at this."""
    return max(
        (project.overrides.apply(config.defaults).payload_bytes for project in config.projects),
        default=config.defaults.payload_bytes,
    )


def write_spool_limits(paths: UserPaths, config: Config) -> None:
    """Publish the few numbers the native adapter needs so it never parses config."""
    atomic_write(
        paths.data / "spool" / "limits.json",
        json.dumps(
            {
                "schema_version": 1,
                "payload_bytes": spool_payload_bytes(config),
                "spool_bytes": SPOOL_BYTES,
                "spool_files": SPOOL_FILES,
            }
        ).encode(),
    )


def launch(paths: UserPaths) -> None:
    atomic_write(paths.runtime / "status.json", b"{}")
    try:
        write_spool_limits(paths, load_config(paths.config))
    except (ConfigError, OSError):
        pass
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
        creationflags=hidden_creationflags(subprocess.CREATE_NEW_PROCESS_GROUP)
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


def resolve_or_auto_register(
    paths: UserPaths, path: Path, *, timeout: float = 10
) -> tuple[Config, Resolution | None]:
    """Resolve a path against the latest config and persist a permitted auto-add."""
    with control_lock(paths, timeout=timeout):
        registry = Registry(load_config(paths.config))
        resolution, added = registry.resolve_or_auto_add(path, timeout=timeout)
        if added:
            save_config(paths.config, registry.config)
            _log(paths, registry.config, "INFO", event="registry", decision="auto_registered")
        elif resolution is None:
            _log(paths, registry.config, "DEBUG", event="registry", decision="unregistered")
        return registry.config, resolution


def enqueue(paths: UserPaths, event: Envelope) -> bool:
    """Adapter entry point; pause and allowlist admission share the control lock."""
    with control_lock(paths, timeout=0.1):
        if desired(paths).get("paused", False):
            _log(paths, None, "DEBUG", event="enqueue", decision="paused")
            return False
        config = load_config(paths.config)
        project = next((item for item in config.projects if item.id == event.project_id), None)
        if project is None:
            _log(
                paths,
                config,
                "DEBUG",
                event="enqueue",
                decision="unregistered",
                event_id=event.event_id,
            )
            return False
        limits = project.overrides.apply(config.defaults)
        try:
            Inbox(paths.project_data(project.id), limits=limits).publish(event)
        except QuotaExceeded:
            _log(
                paths,
                config,
                "WARNING",
                event="enqueue",
                decision="rejected",
                reason="quota",
                project_id=project.id,
                event_id=event.event_id,
            )
            if not alive(paths):
                launch(paths)
            raise
        if not alive(paths):
            launch(paths)
        _log(
            paths,
            config,
            "DEBUG",
            event="enqueue",
            decision="admitted",
            project_id=project.id,
            event_id=event.event_id,
        )
        return True


def request_control(paths: UserPaths, request: dict, *, timeout: float = 10) -> dict:
    """Submit one user control request and wait for the core's durable acknowledgement."""
    request_id = str(uuid4())
    document = {"schema_version": 1, "request_id": request_id, **request}
    with control_lock(paths):
        atomic_write(
            paths.data / "requests" / f"{request_id}.json",
            json.dumps(document, sort_keys=True).encode(),
        )
    start(paths)
    deadline = time.monotonic() + timeout
    acknowledgement = paths.data / "acks" / f"{request_id}.json"
    while time.monotonic() < deadline:
        result = read_json(acknowledgement)
        if result is not None:
            acknowledgement.unlink(missing_ok=True)
            if result.get("request_id") != request_id or result.get("schema_version") != 1:
                raise StorageError("Invalid core acknowledgement")
            if result.get("ok") is not True:
                raise StorageError(str(result.get("message", "Core request failed")))
            value = result.get("result")
            if not isinstance(value, dict):
                raise StorageError("Invalid core acknowledgement")
            return value
        time.sleep(0.05)
    raise StorageError("Timed out waiting for the core acknowledgement")


def _drain_controls(paths: UserPaths, config: Config, *, limit: int = 100) -> bool:
    """Execute bounded user requests in the core; never touch vendor files."""
    directory = paths.data / "requests"
    if not directory.is_dir():
        return False
    projects = {str(project.id): project for project in config.projects}
    activity = False
    for path in sorted(directory.glob("*.json"))[:limit]:
        try:
            if path.is_symlink():
                raise ValueError("Invalid control request")
            data = path.read_bytes()
            if len(data) > CONTROL_REQUEST_BYTES:
                raise ValueError("Oversized control request")
            request = json.loads(data)
            if not isinstance(request, dict) or request.get("schema_version") != 1:
                raise ValueError("Invalid control request")
            request_id = str(UUID(str(request["request_id"])))
            project = projects[str(UUID(str(request["project_id"])))]
            action, provider, session_id = (
                request["action"],
                request["provider"],
                request["session_id"],
            )
            if action not in {"label", "pin", "purge"} or provider not in {"codex", "claude"}:
                raise ValueError("Invalid control request")
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("Invalid control request")
            with Store(paths.project_data(project.id), project.id) as store:
                if action == "label":
                    outcome, task_type = request["outcome"], request.get("task_type")
                    if not isinstance(outcome, str) or not (
                        task_type is None or isinstance(task_type, str)
                    ):
                        raise ValueError("Invalid control request")
                    result = {
                        "label": store.label(
                            provider, session_id, outcome=outcome, task_type=task_type
                        )
                    }
                elif action == "pin":
                    pinned = request.get("pinned")
                    if type(pinned) is not bool:
                        raise ValueError("Invalid control request")
                    store.pin_provider_session(provider, session_id, pinned=pinned)
                    result = {"pinned": pinned}
                else:
                    result = {"deleted_events": store.purge_provider_session(provider, session_id)}
            acknowledgement = {
                "schema_version": 1,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
            _log(
                paths,
                config,
                "INFO",
                event="control",
                decision="acknowledged",
                project_id=project.id,
            )
        except WriterBusy:
            _log(paths, config, "DEBUG", event="control", decision="retry", reason="writer_busy")
            continue
        except (KeyError, OSError, StorageError, ValueError, TypeError) as error:
            request_id = path.stem
            acknowledgement = {
                "schema_version": 1,
                "request_id": request_id,
                "ok": False,
                "message": str(error) or "Invalid control request",
            }
            _log(
                paths,
                config,
                "WARNING",
                event="control",
                decision="rejected",
                error_type=error_code(error),
            )
        atomic_write(
            paths.data / "acks" / f"{request_id}.json", json.dumps(acknowledgement).encode()
        )
        _discard(path)
        activity = True
    return activity


def _admit(paths: UserPaths, event: Envelope, config: Config) -> bool:
    """Publish one envelope under the control lock. The caller is the running
    daemon, so unlike `enqueue` this never self-launches. Raises on quota/busy."""
    with control_lock(paths, timeout=0.1):
        if desired(paths).get("paused", False):
            return False
        project = next((item for item in config.projects if item.id == event.project_id), None)
        if project is None:
            return False
        limits = project.overrides.apply(config.defaults)
        Inbox(paths.project_data(project.id), limits=limits).publish(event)
        return True


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _drain_spool(paths: UserPaths, config: Config, *, limit: int = 100) -> bool:
    """Resolve, build, and admit native adapter spool records.

    The adapter writes redacted-but-unresolved records; checkout resolution,
    envelope construction, and per-project quota all happen here. Returns whether
    any record was processed.
    """
    from agent_watchdog.hooks import EVENTS, build_envelope, warn_unknown_fields
    from agent_watchdog.privacy import sanitize

    spool = paths.data / "spool"
    if not spool.is_dir():
        return False
    payload_bytes = spool_payload_bytes(config)
    registry = Registry(config)
    activity = False
    processed = 0
    for path in sorted(spool.glob("*.json")):
        if path.name == "limits.json":
            continue
        if processed >= limit:
            break
        processed += 1
        try:
            if path.is_symlink():
                _log(paths, config, "WARNING", event="spool", decision="discarded", reason="linked")
                _discard(path)
                activity = True
                continue
            with path.open("rb") as stream:
                data = stream.read(payload_bytes + 1)
            if len(data) > payload_bytes:
                raise ValueError("Oversized spool record")
            record = json.loads(data)
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                raise ValueError("Invalid spool record")
            provider = record["provider"]
            payload = record["input"]
            cwd = record["cwd"]
            if provider not in EVENTS or not isinstance(payload, dict):
                raise ValueError("Invalid spool record")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError("Invalid spool cwd")
            event_id = UUID(str(record["event_id"]))
            received_at = datetime.fromisoformat(record["received_at"])
        except (OSError, ValueError, KeyError, TypeError):
            resources.count_loss(paths.data, "invalid")
            _log(paths, config, "WARNING", event="spool", decision="discarded", reason="invalid")
            _discard(path)
            activity = True
            continue
        resolution = registry.resolve(Path(cwd), timeout=0.25)
        if resolution is None and config.auto_add_projects:
            config, resolution = resolve_or_auto_register(paths, Path(cwd), timeout=0.25)
            registry = Registry(config)
        if resolution is None:
            _log(paths, config, "DEBUG", event="spool", decision="unregistered", event_id=event_id)
            _discard(path)
            activity = True
            continue
        project = next(item for item in config.projects if item.id == resolution.project_id)
        limits = project.overrides.apply(config.defaults)
        try:
            event = build_envelope(
                payload,
                resolution,
                limits,
                provider,
                event_id=event_id,
                received_at=received_at,
            )
            warn_unknown_fields(
                paths,
                config.defaults,
                payload,
                provider,
                component="daemon",
                event="spool",
                project_id=project.id,
                event_id=event_id,
            )
            admitted = _admit(paths, sanitize(event), config)
        except QuotaExceeded:
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="discarded",
                reason="quota",
                project_id=project.id,
                event_id=event_id,
            )
            _discard(path)  # Inbox.publish already counted the project loss.
            activity = True
            continue
        except WriterBusy:
            _log(
                paths,
                config,
                "DEBUG",
                event="spool",
                decision="retry",
                reason="writer_busy",
                project_id=project.id,
                event_id=event_id,
            )
            continue  # Transient; retry on the next poll.
        except (ValidationError, RejectedEvent, StorageError, ValueError, KeyError, TypeError):
            resources.count_loss(paths.data, "invalid")
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="discarded",
                reason="invalid",
                project_id=project.id,
                event_id=event_id,
            )
            _discard(path)
            activity = True
            continue
        if not admitted and desired(paths).get("paused", False):
            _log(
                paths,
                config,
                "DEBUG",
                event="spool",
                decision="paused",
                project_id=project.id,
                event_id=event_id,
            )
            continue  # Keep the record for the next unpaused poll.
        if admitted:
            _capture_diff_snapshot(event, resolution.root, project.id, paths)
            _log(
                paths,
                config,
                "DEBUG",
                event="spool",
                decision="admitted",
                project_id=project.id,
                event_id=event_id,
            )
        # Admitted, or the project was unregistered mid-drain: either way, drop it.
        # A failed unlink replays the same event_id, which Store.put deduplicates.
        _discard(path)
        activity = True
    return activity


def run(paths: UserPaths) -> int:
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as owner:
            owner.enter_context(writer_lock(paths.data / "daemon.lock"))
            _log(paths, None, "INFO", event="lifecycle", decision="started")
            _poll(paths, owner)
    except WriterBusy:
        _log(paths, None, "DEBUG", event="lifecycle", decision="already_running")
        return 0
    _log(paths, None, "INFO", event="lifecycle", decision="stopped")
    return 0


def _poll(paths: UserPaths, owner: ExitStack) -> None:
    instance_id = str(uuid4())
    delay = 0.25
    maintenance: dict[str, float] = {}
    spool_mtime: float | None = None
    while True:
        config: Config | None = None
        control = desired(paths)
        if control.get("paused", False):
            # Release ownership under the same lock as start, avoiding a lost restart.
            with control_lock(paths):
                if desired(paths).get("paused", False):
                    _log(paths, None, "INFO", event="lifecycle", decision="paused")
                    owner.close()
                    return
            continue
        errors = []
        activity = False
        try:
            config = load_config(paths.config)
            try:
                mtime = paths.config.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != spool_mtime:
                try:
                    write_spool_limits(paths, config)
                    spool_mtime = mtime
                    _log(paths, config, "INFO", event="configuration", decision="reloaded")
                except OSError:
                    _log(paths, config, "WARNING", event="configuration", decision="deferred")
                    pass
            if _drain_spool(paths, config):
                activity = True
            if _drain_controls(paths, config):
                activity = True
            for project in config.projects:
                try:
                    root = paths.project_data(project.id)
                    limits = project.overrides.apply(config.defaults)
                    with Store(root, project.id, limits=limits) as store:
                        if time.monotonic() >= maintenance.get(str(project.id), 0):
                            store.maintain()
                            maintenance[str(project.id)] = time.monotonic() + 60
                        result = Inbox(root, limits=limits).drain(store)
                        try:
                            from agent_watchdog.transcripts import enrich

                            activity |= bool(enrich(store))
                        except (OSError, StorageError, ValueError):
                            if len(errors) < 32:
                                errors.append(str(project.id))
                            _log(
                                paths,
                                config,
                                "WARNING",
                                event="enrichment",
                                decision="failed",
                                project_id=project.id,
                            )
                        if resources.available(root, limits) < 65536 and len(errors) < 32:
                            errors.append(str(project.id))
                    activity |= bool(
                        result.inserted + result.duplicates + result.quarantined + result.discarded
                    )
                except (OSError, StorageError, sqlite3.Error):
                    if len(errors) < 32:
                        errors.append(str(project.id))
                    _log(
                        paths,
                        config,
                        "WARNING",
                        event="project",
                        decision="degraded",
                        project_id=project.id,
                    )
        except ConfigError:
            errors.append("configuration")
            _log(paths, None, "ERROR", event="configuration", decision="invalid")
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
        _log(
            paths,
            config,
            "DEBUG",
            event="poll",
            decision="active" if activity else "idle",
            count=len(errors),
        )
        time.sleep(delay)
