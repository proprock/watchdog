"""One local polling process; durable desired state and no PID-based signals."""

import json
import os
import secrets
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from agent_watchdog import resources
from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.config import Config, ConfigError, Limits, UserPaths, load_config, save_config
from agent_watchdog.diagnostics import Level, emit, error_code
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry, RegistryError, Resolution
from agent_watchdog.rules.api import Decision
from agent_watchdog.rules.engine import decide, subscriptions
from agent_watchdog.state import SessionState, StateKey
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
# WD-141: a session's findings are recomputed at most this often, and a tick spends
# at most this long on it (the remaining sessions wait for the next tick).
FINDINGS_INTERVAL_SECONDS = 60.0
FINDINGS_BUDGET_SECONDS = 0.2


class _ConfigCache:
    """Skip reparsing ``config.toml`` when it has not changed.

    ``load_config`` is called on every poll tick and every policy-socket
    request; config edits are, in practice, orders of magnitude rarer than
    either. This never requires an explicit reload -- a real edit is still
    picked up on the very next call, exactly as calling ``load_config``
    directly would behave -- it only skips the parse/validate cost when nothing
    changed, keyed by path so distinct config files (as in tests) never share
    a stale entry. Thread-safe: the poll loop and the policy-socket handler
    thread both call this.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: dict[str, tuple[float, int, Config]] = {}

    def load(self, path: Path) -> Config:
        key = str(path)
        with self._lock:
            try:
                info = path.stat()
            except OSError:
                self._cached.pop(key, None)
                return load_config(path)
            cached = self._cached.get(key)
            if cached is not None and cached[:2] == (info.st_mtime, info.st_size):
                return cached[2]
            config = load_config(path)
            self._cached[key] = (info.st_mtime, info.st_size, config)
            return config


_config_cache = _ConfigCache()


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
    detail: str | None = None,
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
        detail=detail,
    )


def _capture_diff_snapshot(
    checkout_id: UUID | None,
    checkout: Path,
    project_id: UUID,
    paths: UserPaths,
    config: Config,
    logged_unknown: set[tuple[UUID, str]],
) -> None:
    """Run the bounded Git read in the core, never synchronously in a hook.

    An unknown state records no snapshot, which silences ``diff_oscillation`` for
    the checkout; it is logged once per checkout and reason until a read fits the
    bounds again.
    """
    if checkout_id is None:
        return
    from agent_watchdog.analysis import CheckoutUnknown, git_diff_fingerprint

    now = datetime.now(UTC)
    try:
        with Store(paths.project_data(project_id), project_id) as store:
            if not store.diff_due(checkout_id, now=now):
                return
            fingerprint = git_diff_fingerprint(checkout)
            if isinstance(fingerprint, CheckoutUnknown):
                if (checkout_id, fingerprint.reason) not in logged_unknown:
                    logged_unknown.add((checkout_id, fingerprint.reason))
                    _log(
                        paths,
                        config,
                        "WARNING",
                        event="checkout",
                        decision="unavailable",
                        error_type=fingerprint.reason,
                        project_id=project_id,
                        detail=f"{checkout}: {fingerprint.detail}",
                    )
                return
            logged_unknown.difference_update(
                {item for item in logged_unknown if item[0] == checkout_id}
            )
            store.record_diff_snapshot(checkout_id, *fingerprint, observed_at=now)
    except (OSError, StorageError, ValueError):
        return


def _record_findings_failures(
    paths: UserPaths,
    config: Config | None,
    project_id: UUID,
    failures: list[Exception],
    logged_failures: set[tuple[str, str]],
) -> None:
    """Log each findings-refresh failure once per episode; the project is not degraded by it."""
    current = {(str(project_id), error_code(error)) for error in failures}
    for _, code in current - logged_failures:
        _log(
            paths,
            config,
            "WARNING",
            event="findings",
            decision="unavailable",
            error_type=code,
            project_id=project_id,
        )
    logged_failures.difference_update(
        {item for item in logged_failures if item[0] == str(project_id) and item not in current}
    )
    logged_failures.update(current)


def _record_enrichment_failures(
    paths: UserPaths,
    config: Config,
    project_id: UUID,
    failures: tuple[str, ...],
    logged_failures: set[tuple[str, str]],
    *,
    detail: str | None = None,
) -> bool:
    """Log newly active reader failures and report whether the project is degraded."""
    from agent_watchdog.transcripts import FAILURE_CODES

    current_failures = {
        (str(project_id), code if code in FAILURE_CODES else "transcript_enrichment_invalid")
        for code in failures
    }
    for _, code in current_failures - logged_failures:
        _log(
            paths,
            config,
            "WARNING",
            event="enrichment",
            decision="unavailable",
            error_type=code,
            project_id=project_id,
            detail=detail,
        )
    logged_failures.difference_update(
        {
            item
            for item in logged_failures
            if item[0] == str(project_id) and item not in current_failures
        }
    )
    logged_failures.update(current_failures)
    return bool(current_failures)


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
    queues: dict[str, object] = {}
    try:
        loss_report = {"adapter": resources.losses(paths.data)}
        config = load_config(paths.config)
        for project in config.projects[:32]:
            loss_report[str(project.id)] = resources.losses(paths.project_data(project.id))
        queues = _queue_status(paths, config)
    except (OSError, ValueError):
        loss_report = {"unavailable": True}
        queues = {"unavailable": True}
        if running and state != "paused":
            state = "degraded"
    return report | {
        "state": state,
        "alive": running,
        "request_id": control.get("request_id"),
        "losses": loss_report,
        "queues": queues,
    }


def _queue_snapshot(
    directory: Path, *, byte_limit: int, file_limit: int | None, skip: str | None = None
) -> dict:
    entries = [
        path
        for path in directory.glob("*.json")
        if path.is_file() and (skip is None or path.name != skip)
    ]
    used = sum(path.stat().st_size for path in entries)
    return {
        "files": len(entries),
        "bytes": used,
        "file_limit": file_limit,
        "byte_limit": byte_limit,
    }


def _queue_status(paths: UserPaths, config: Config) -> dict[str, object]:
    projects: dict[str, object] = {}
    for project in config.projects[:32]:
        limits = project.overrides.apply(config.defaults)
        projects[str(project.id)] = _queue_snapshot(
            paths.project_data(project.id) / "inbox",
            byte_limit=limits.inbox_bytes,
            file_limit=None,
        )
    return {
        "spool": _queue_snapshot(
            paths.data / "spool",
            byte_limit=SPOOL_BYTES,
            file_limit=SPOOL_FILES,
            skip="limits.json",
        ),
        "projects": projects,
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
                "pipeline_telemetry": config.pipeline_telemetry,
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
            if config.pipeline_telemetry:
                observed_at = datetime.now(UTC).isoformat()
                event = event.model_copy(
                    update={
                        "delivery": event.delivery
                        | {
                            "inbox_enqueued_at": observed_at,
                            "inbox_occupancy": _queue_snapshot(
                                paths.project_data(project.id) / "inbox",
                                byte_limit=limits.inbox_bytes,
                                file_limit=None,
                            ),
                        }
                    }
                )
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
            action = request["action"]
            if action not in {"label", "pin", "purge", "verdict", "checkout_verdict"}:
                raise ValueError("Invalid control request")
            if action == "checkout_verdict":
                # A checkout is shared by both providers, so this action carries no session.
                checkout_id, provider, session_id = _checkout_target(request), "", ""
            else:
                checkout_id = ""
                provider, session_id = _session_target(request)
            with Store(paths.project_data(project.id), project.id) as store:
                if action == "checkout_verdict":
                    rule, rule_version, fingerprint, verdict, note = _verdict_fields(request)
                    result = {
                        "verdict": store.checkout_finding_verdict(
                            checkout_id,
                            rule=rule,
                            rule_version=rule_version,
                            fingerprint=fingerprint,
                            verdict=verdict,
                            note=note,
                        )
                    }
                elif action == "label":
                    outcome, task_type = request["outcome"], request.get("task_type")
                    progress_state = request.get("progress_state")
                    reviewer_note = request.get("reviewer_note")
                    if not isinstance(outcome, str) or not (
                        task_type is None or isinstance(task_type, str)
                    ):
                        raise ValueError("Invalid control request")
                    if not (progress_state is None or isinstance(progress_state, str)) or not (
                        reviewer_note is None or isinstance(reviewer_note, str)
                    ):
                        raise ValueError("Invalid control request")
                    result = {
                        "label": store.label(
                            provider,
                            session_id,
                            outcome=outcome,
                            task_type=task_type,
                            progress_state=progress_state,
                            reviewer_note=reviewer_note,
                        )
                    }
                elif action == "verdict":
                    rule, rule_version, fingerprint, verdict, note = _verdict_fields(request)
                    result = {
                        "verdict": store.finding_verdict(
                            provider,
                            session_id,
                            rule=rule,
                            rule_version=rule_version,
                            fingerprint=fingerprint,
                            verdict=verdict,
                            note=note,
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
                detail=str(error),
            )
        atomic_write(
            paths.data / "acks" / f"{request_id}.json", json.dumps(acknowledgement).encode()
        )
        _discard(path)
        activity = True
    return activity


def _session_target(request: Mapping[str, object]) -> tuple[str, str]:
    provider, session_id = request["provider"], request["session_id"]
    if provider not in {"codex", "claude"}:
        raise ValueError("Invalid control request")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Invalid control request")
    return str(provider), session_id


def _checkout_target(request: Mapping[str, object]) -> str:
    checkout_id = request.get("checkout_id")
    if not isinstance(checkout_id, str) or not checkout_id.strip():
        raise ValueError("Invalid control request")
    return checkout_id


def _verdict_fields(request: Mapping[str, object]) -> tuple[str, str, str, str, str | None]:
    rule, rule_version = request.get("rule"), request.get("rule_version")
    fingerprint, verdict = request.get("fingerprint"), request.get("verdict")
    note = request.get("note")
    if not all(isinstance(field, str) for field in (rule, rule_version, fingerprint, verdict)):
        raise ValueError("Invalid control request")
    if not (note is None or isinstance(note, str)):
        raise ValueError("Invalid control request")
    return str(rule), str(rule_version), str(fingerprint), str(verdict), note


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
        if config.pipeline_telemetry:
            observed_at = datetime.now(UTC).isoformat()
            event = event.model_copy(
                update={
                    "delivery": event.delivery
                    | {
                        "inbox_enqueued_at": observed_at,
                        "inbox_occupancy": _queue_snapshot(
                            paths.project_data(project.id) / "inbox",
                            byte_limit=limits.inbox_bytes,
                            file_limit=None,
                        ),
                    }
                }
            )
        Inbox(paths.project_data(project.id), limits=limits).publish(event)
        return True


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _drain_spool(
    paths: UserPaths,
    config: Config,
    *,
    limit: int = 100,
    logged_unknown_checkouts: set[tuple[UUID, str]] | None = None,
) -> bool:
    """Resolve, build, and admit native adapter spool records.

    The adapter writes raw unresolved records; checkout resolution,
    envelope construction, and per-project quota all happen here. Returns whether
    any record was processed. ``logged_unknown_checkouts`` carries the
    once-per-episode fingerprint log state across calls.
    """
    from agent_watchdog.hooks import EVENTS, build_envelope, warn_unknown_fields

    if logged_unknown_checkouts is None:
        logged_unknown_checkouts = set()

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
                resources.count_loss(paths.data, "invalid")
                _log(
                    paths,
                    config,
                    "WARNING",
                    event="spool",
                    decision="discarded",
                    reason="oversized",
                )
                _discard(path)
                activity = True
                continue
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
            delivery = record.get("delivery", {})
            if not isinstance(delivery, dict):
                raise ValueError("Invalid spool delivery telemetry")
        except (OSError, ValueError, KeyError, TypeError) as error:
            resources.count_loss(paths.data, "invalid")
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="discarded",
                reason="invalid",
                error_type=error_code(error),
                detail=str(error),
            )
            _discard(path)
            activity = True
            continue
        try:
            resolution = registry.resolve(Path(cwd), timeout=0.25)
            if resolution is None and config.auto_add_projects:
                config, resolution = resolve_or_auto_register(paths, Path(cwd), timeout=0.25)
                registry = Registry(config)
        except RegistryError as error:
            # A resolution failure must never stop the daemon: one record from a deleted
            # directory would otherwise kill it on every start and stall all records behind it.
            if not Path(cwd).exists():
                _log(
                    paths,
                    config,
                    "WARNING",
                    event="spool",
                    decision="discarded",
                    reason="unresolvable_cwd",
                    error_type=error_code(error),
                )
                _discard(path)
                activity = True
            elif (event_id, "unresolvable_cwd") not in logged_unknown_checkouts:
                # The directory exists, so the failure may be transient (a Git timeout):
                # keep the record and retry on the next pass, logging it once.
                logged_unknown_checkouts.add((event_id, "unresolvable_cwd"))
                _log(
                    paths,
                    config,
                    "WARNING",
                    event="spool",
                    decision="deferred",
                    reason="unresolvable_cwd",
                    error_type=error_code(error),
                )
            continue
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
            if config.pipeline_telemetry:
                event = event.model_copy(
                    update={
                        "delivery": delivery | {"spool_drained_at": datetime.now(UTC).isoformat()}
                    }
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
            admitted = _admit(paths, event, config)
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
        except (
            ValidationError,
            RejectedEvent,
            StorageError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            resources.count_loss(paths.data, "invalid")
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="discarded",
                reason="invalid",
                error_type=error_code(error),
                detail=str(error),
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
            _capture_diff_snapshot(
                event.checkout_id,
                resolution.root,
                project.id,
                paths,
                config,
                logged_unknown_checkouts,
            )
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


_POLICY_SCHEMA_VERSION = 2
_POLICY_TIMEOUT_SECONDS = 1.0
# What the adapter waits for one decision. It must stay well inside the fixed
# two-second hook timeout the installer writes.
_POLICY_DECISION_TIMEOUT_MS = 300
# The request carries the adapter's whole stdin (capped at the spool payload
# limit) plus a token, ids, and a re-encoding of the same input. The slack
# covers that envelope so a maximal prompt is still answered, not silently
# dropped (which would fail open and disable the rule).
_POLICY_REQUEST_OVERHEAD_BYTES = 64 * 1024


def _policy_request_bytes(paths: UserPaths) -> int:
    try:
        payload_bytes = spool_payload_bytes(load_config(paths.config))
    except (ConfigError, OSError):
        payload_bytes = Limits().payload_bytes
    return payload_bytes + _POLICY_REQUEST_OVERHEAD_BYTES


def _record_control(
    paths: UserPaths,
    provider: str,
    hook_input: Mapping[str, object],
    event_id: UUID,
    decision: Decision,
) -> None:
    """Record one action the channel answered with, as a ``control`` event.

    Runs on a handler thread, so it never opens a ``Store`` (the poll thread
    holds the writer lock); ``_admit`` publishes to the project inbox, which
    ``_poll`` drains. The daemon cannot see whether the adapter rendered the
    answer in time, so "delivered" means "sent", and a failure here is only
    logged: the decision was already returned.
    """
    if decision.action == "allow" or decision.project_id is None:
        return
    session_id = hook_input.get("session_id")
    control_id = uuid5(
        NAMESPACE_URL, f"watchdog|control|{event_id}|{decision.rule}|{decision.action}"
    )
    try:
        config = _config_cache.load(paths.config)
        envelope = Envelope(
            event_id=control_id,
            provider=provider,
            project_id=decision.project_id,
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            native_event_id=f"control:{control_id.hex}",
            kind="control",
            source="daemon",
            payload={
                provider: {
                    "rule": decision.rule,
                    "rule_version": decision.rule_version,
                    "action": decision.action,
                    "reason": decision.reason,
                    # The text the model was sent: a reason, or injected context.
                    "text": decision.reason or decision.context,
                    "evidence_ids": [str(event_id)],
                    "delivered_at": datetime.now(UTC).isoformat(),
                }
            },
        )
        _admit(paths, envelope, config)
    except (ConfigError, ValidationError, StorageError, OSError) as error:
        _log(
            paths,
            None,
            "WARNING",
            event="control",
            decision="unrecorded",
            error_type=error_code(error),
        )


def _decision_response(decision: Decision) -> dict[str, object]:
    # `log` is recorded by the daemon and means nothing to the adapter.
    response: dict[str, object] = {
        "schema_version": _POLICY_SCHEMA_VERSION,
        "action": "allow" if decision.action == "log" else decision.action,
    }
    if decision.action == "log":
        return response
    if decision.reason is not None:
        response["reason"] = decision.reason
    if decision.updated_input is not None:
        response["updated_input"] = decision.updated_input
    if decision.context is not None:
        response["context"] = decision.context
    return response


class _PolicyRequestHandler(socketserver.StreamRequestHandler):
    """Handles exactly one bounded request per connection, then closes it.

    Every failure -- unreadable, oversized, malformed, wrong version or token,
    a rule that raises -- closes the connection without a response, which the
    adapter reads as "no opinion". The channel never fails closed.
    """

    server: "_PolicyServer"

    def _session(self, provider: str, hook_input: Mapping[str, object]) -> SessionState | None:
        """The daemon's in-memory state of the calling agent; None until it is observed."""
        session_id, agent_id = hook_input.get("session_id"), hook_input.get("agent_id")
        if self.server.watchdog_sessions is None or not isinstance(session_id, str):
            return None
        return self.server.watchdog_sessions.session(
            provider, session_id, agent_id if isinstance(agent_id, str) else ""
        )

    def _rule_failed(self, name: str, error: Exception) -> None:
        """Log a rule's failure once per daemon lifetime; the other rules still answer."""
        if name in self.server.watchdog_failed_rules:
            return
        self.server.watchdog_failed_rules.add(name)
        _log(
            self.server.watchdog_paths,
            None,
            "WARNING",
            event="control",
            decision="rule_error",
            error_type=error_code(error),
            detail=name,
        )

    def handle(self) -> None:
        self.connection.settimeout(_POLICY_TIMEOUT_SECONDS)
        try:
            line = self.rfile.readline(self.server.watchdog_request_bytes)
        except OSError:
            return
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(request, dict) or request.get("schema_version") != _POLICY_SCHEMA_VERSION:
            return
        if not secrets.compare_digest(str(request.get("token", "")), self.server.watchdog_token):
            return
        provider, hook_input = request.get("provider"), request.get("input")
        if provider not in {"claude", "codex"} or not isinstance(hook_input, dict):
            return
        try:
            event_id = UUID(str(request.get("event_id")))
        except ValueError:
            return
        paths = self.server.watchdog_paths
        try:
            config = _config_cache.load(paths.config)
            decision = decide(
                paths,
                config,
                str(provider),
                hook_input,
                session=self._session(str(provider), hook_input),
                on_error=self._rule_failed,
            )
        except ConfigError:
            return
        except Exception as error:  # noqa: BLE001 - a rule bug must fail open, not kill the handler
            _log(
                paths,
                None,
                "WARNING",
                event="control",
                decision="rule_error",
                error_type=error_code(error),
            )
            return
        try:
            self.wfile.write(json.dumps(_decision_response(decision)).encode())
            # The adapter reads to end-of-stream; signal it now so recording the
            # control event below never adds to its wait.
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        _record_control(paths, str(provider), hook_input, event_id, decision)


class _PolicyServer(socketserver.ThreadingTCPServer):
    # Deliberately NOT allow_reuse_address: with an ephemeral port this buys
    # nothing, and on Windows it would let another process bind the same port
    # while this one is still listening -- whoever received the connection
    # could answer "deny", and a false deny is the one Watchdog failure that
    # would actually stop the harness.
    daemon_threads = True
    watchdog_paths: UserPaths
    watchdog_token: str
    watchdog_request_bytes: int
    watchdog_sessions: "_SessionTracker | None"
    watchdog_failed_rules: set[str]


class _Channel:
    """The published discovery file, kept in step with the rules that may run.

    ``sync`` runs on every poll tick: it rewrites ``socket.json`` only when the
    subscriptions changed, so approving, editing or disabling a rule takes
    effect without a restart. A failed rewrite keeps the previous file, which
    can only subscribe to more than is needed (the engine answers ``allow``
    for it) and never to something an adapter would act on without a rule.
    """

    def __init__(self, paths: UserPaths, document: dict[str, object] | None = None) -> None:
        self._paths = paths
        self._document = document
        self._failing = False

    def sync(self, config: Config) -> None:
        if self._document is None:
            return
        try:
            wanted = subscriptions(self._paths, config)
            if wanted == self._document["subscriptions"]:
                self._failing = False
                return
            document = {**self._document, "subscriptions": wanted}
            atomic_write(self._paths.data / "policy" / "socket.json", json.dumps(document).encode())
        except Exception as error:  # noqa: BLE001 - a rule-loading bug must not stop the poll loop
            if not self._failing:
                _log(
                    self._paths,
                    config,
                    "WARNING",
                    event="lifecycle",
                    decision="degraded",
                    error_type=error_code(error),
                )
            self._failing = True
            return
        self._failing = False
        self._document = document


def _initial_subscriptions(paths: UserPaths) -> list[dict[str, str | None]]:
    try:
        return subscriptions(paths, load_config(paths.config))
    except (ConfigError, OSError):
        return []


@contextmanager
def _policy_server(
    paths: UserPaths, sessions: "_SessionTracker | None" = None
) -> Iterator[_Channel]:
    """Run the WD-014 ``intervene`` decision socket for one daemon lifetime.

    Loopback-only (binding wider triggers a Windows Firewall prompt for no
    benefit, since only this machine's own adapter ever calls it) and
    published, with a per-instance random token, to ``policy/socket.json`` --
    the same discovery-file pattern as ``spool/limits.json``. A stale file
    left behind after an unclean exit, or a since-reused port, can only ever
    fail the token check or connect to an unrelated listener that will not
    answer this protocol; either way the adapter fails open, never a false
    deny. Removed on every exit path, clean or not, via the caller's ExitStack.

    A bind or publish failure (a sandboxed/job-object socket denial, a full
    disk, antivirus interference) degrades to no policy socket at all rather
    than raising: this optional capability must never take down the shared
    daemon that Codex observation also depends on -- ``run()`` only recovers
    from ``WriterBusy``, so any other exception here would stop the daemon
    entirely, breaking the WD-022b "Claude-specific failures do not break
    Codex" acceptance criterion.
    """
    socket_path = paths.data / "policy" / "socket.json"
    try:
        server = _PolicyServer(("127.0.0.1", 0), _PolicyRequestHandler)
    except OSError as error:
        _log(
            paths,
            None,
            "WARNING",
            event="lifecycle",
            decision="degraded",
            error_type=error_code(error),
        )
        yield _Channel(paths)
        return
    server.watchdog_paths = paths
    server.watchdog_sessions = sessions
    server.watchdog_failed_rules = set()
    server.watchdog_token = secrets.token_hex(16)
    server.watchdog_request_bytes = _policy_request_bytes(paths)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    document: dict[str, object] = {
        "schema_version": _POLICY_SCHEMA_VERSION,
        "pid": os.getpid(),
        "port": server.server_address[1],
        "token": server.watchdog_token,
        "timeout_ms": _POLICY_DECISION_TIMEOUT_MS,
        "max_request_bytes": server.watchdog_request_bytes,
        "subscriptions": _initial_subscriptions(paths),
    }
    try:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(socket_path, json.dumps(document).encode())
    except OSError as error:
        _log(
            paths,
            None,
            "WARNING",
            event="lifecycle",
            decision="degraded",
            error_type=error_code(error),
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        try:
            socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        yield _Channel(paths)
        return
    try:
        yield _Channel(paths, document)
    finally:
        try:
            socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run(paths: UserPaths) -> int:
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as owner:
            owner.enter_context(writer_lock(paths.data / "daemon.lock"))
            sessions = _SessionTracker()
            channel = owner.enter_context(_policy_server(paths, sessions))
            _log(paths, None, "INFO", event="lifecycle", decision="started")
            _poll(paths, owner, sessions, channel)
    except WriterBusy:
        _log(paths, None, "DEBUG", event="lifecycle", decision="already_running")
        return 0
    _log(paths, None, "INFO", event="lifecycle", decision="stopped")
    return 0


class _SessionTracker:
    """The daemon's session state (WD-141): live states and the findings refresh schedule.

    The poll thread is the only writer; the decision-channel threads only read
    ``session``.  States are immutable, so a reader never sees a half-applied one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[StateKey, SessionState] = {}
        self._owned: dict[str, set[StateKey]] = {}
        # project -> session -> whether it ended (refresh without waiting out the interval).
        self._due: dict[str, dict[tuple[str, str], bool]] = {}
        self._refreshed: dict[tuple[str, str, str], float] = {}

    def session(self, provider: str, session_id: str, agent_id: str = "") -> SessionState | None:
        with self._lock:
            return self._states.get((provider, session_id, agent_id))

    def knows(self, project_id: str) -> bool:
        return project_id in self._owned

    def load(self, project_id: str, store: Store) -> None:
        """Replace one project's states with the stored ones: first sight, or after retention."""
        stored = store.session_states()
        with self._lock:
            for key in self._owned.get(project_id, ()):
                self._states.pop(key, None)
            self._states.update(stored)
            self._owned[project_id] = set(stored)
        due = self._due.setdefault(project_id, {})
        for session in store.pending_sessions():
            due.setdefault(session, False)

    def absorb(self, project_id: str, touched: Mapping[StateKey, SessionState]) -> None:
        """Take over the states this tick's writes produced and schedule their sessions."""
        touched = dict(touched)
        with self._lock:
            self._states.update(touched)
            self._owned.setdefault(project_id, set()).update(touched)
        due = self._due.setdefault(project_id, {})
        for (provider, session_id, _), state in touched.items():
            session = (provider, session_id)
            due[session] = due.get(session, False) or state.ended_at is not None

    def refresh(
        self, project_id: str, store: Store, *, now: float, budget: float = FINDINGS_BUDGET_SECONDS
    ) -> tuple[int, list[Exception]]:
        """Refresh due sessions, longest-waiting first, until the budget is spent.

        At least one session is refreshed per call, so a slow session cannot starve
        itself.  A failed session waits out the interval before it is retried.
        """
        due = self._due.get(project_id, {})

        def waited(session: tuple[str, str]) -> float:
            return self._refreshed.get((project_id, *session), float("-inf"))

        ready = sorted(
            (
                session
                for session, ended in due.items()
                if ended or now - waited(session) >= FINDINGS_INTERVAL_SECONDS
            ),
            key=waited,
        )
        deadline = time.monotonic() + budget
        snapshot_cache: dict[frozenset[str], list[dict]] = {}
        done = 0
        failures: list[Exception] = []
        for provider, session_id in ready:
            if done + len(failures) and time.monotonic() >= deadline:
                break
            self._refreshed[project_id, provider, session_id] = now
            try:
                store.refresh_session_findings(provider, session_id, snapshot_cache=snapshot_cache)
            except (OSError, StorageError, sqlite3.Error, ValueError) as error:
                failures.append(error)
                continue
            del due[provider, session_id]
            done += 1
        return done, failures


def _poll(
    paths: UserPaths,
    owner: ExitStack,
    sessions: _SessionTracker | None = None,
    channel: _Channel | None = None,
) -> None:
    sessions = sessions or _SessionTracker()
    channel = channel or _Channel(paths)
    logged_findings_failures: set[tuple[str, str]] = set()
    instance_id = str(uuid4())
    delay = 0.25
    maintenance: dict[str, float] = {}
    logged_enrichment_failures: set[tuple[str, str]] = set()
    logged_inert_enrichment: set[str] = set()
    logged_unknown_checkouts: set[tuple[UUID, str]] = set()
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
        transcript_failures: dict[str, list[str]] = {}
        activity = False
        try:
            config = _config_cache.load(paths.config)
            try:
                mtime = paths.config.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != spool_mtime:
                try:
                    write_spool_limits(paths, config)
                    spool_mtime = mtime
                    _log(paths, config, "INFO", event="configuration", decision="reloaded")
                except OSError as error:
                    _log(
                        paths,
                        config,
                        "WARNING",
                        event="configuration",
                        decision="deferred",
                        error_type=error_code(error),
                        detail=str(error),
                    )
                    pass
            channel.sync(config)
            if _drain_spool(paths, config, logged_unknown_checkouts=logged_unknown_checkouts):
                activity = True
            if _drain_controls(paths, config):
                activity = True
            for project in config.projects:
                try:
                    root = paths.project_data(project.id)
                    limits = project.overrides.apply(config.defaults)
                    with Store(root, project.id, limits=limits) as store:
                        maintained = time.monotonic() >= maintenance.get(str(project.id), 0)
                        if maintained:
                            store.maintain()
                            maintenance[str(project.id)] = time.monotonic() + 60
                        if maintained or not sessions.knows(str(project.id)):
                            sessions.load(str(project.id), store)
                        result = Inbox(root, limits=limits).drain(
                            store, pipeline_telemetry=config.pipeline_telemetry
                        )
                        try:
                            from agent_watchdog.transcripts import enrich, failure_code

                            enrichment = enrich(
                                store,
                                active_failure_since=datetime.now(UTC)
                                - timedelta(minutes=limits.transcript_failure_minutes),
                            )
                            activity |= bool(enrichment)
                            if enrichment.active_failures:
                                transcript_failures[str(project.id)] = list(
                                    enrichment.active_failures
                                )
                            degraded = _record_enrichment_failures(
                                paths,
                                config,
                                project.id,
                                enrichment.active_failures,
                                logged_enrichment_failures,
                            )
                            if enrichment.inert:
                                if str(project.id) not in logged_inert_enrichment:
                                    _log(
                                        paths,
                                        config,
                                        "WARNING",
                                        event="enrichment",
                                        decision="inert",
                                        reason="usage_seen_unstored",
                                        project_id=project.id,
                                    )
                                    logged_inert_enrichment.add(str(project.id))
                                degraded = True
                            else:
                                logged_inert_enrichment.discard(str(project.id))
                            if degraded and len(errors) < 32:
                                errors.append(str(project.id))
                        except (OSError, StorageError, ValueError) as error:
                            code = failure_code(error)
                            transcript_failures[str(project.id)] = [code]
                            if (
                                _record_enrichment_failures(
                                    paths,
                                    config,
                                    project.id,
                                    (code,),
                                    logged_enrichment_failures,
                                    detail=str(error),
                                )
                                and len(errors) < 32
                            ):
                                errors.append(str(project.id))
                        sessions.absorb(str(project.id), store.touched)
                        refreshed, failures = sessions.refresh(
                            str(project.id), store, now=time.monotonic()
                        )
                        activity |= bool(refreshed)
                        _record_findings_failures(
                            paths, config, project.id, failures, logged_findings_failures
                        )
                        if resources.available(root, limits) < 65536 and len(errors) < 32:
                            errors.append(str(project.id))
                    activity |= bool(
                        result.inserted + result.duplicates + result.quarantined + result.discarded
                    )
                except (OSError, StorageError, sqlite3.Error) as error:
                    if len(errors) < 32:
                        errors.append(str(project.id))
                    _log(
                        paths,
                        config,
                        "WARNING",
                        event="project",
                        decision="degraded",
                        error_type=error_code(error),
                        detail=str(error),
                        project_id=project.id,
                    )
        except ConfigError as error:
            errors.append("configuration")
            _log(
                paths,
                None,
                "ERROR",
                event="configuration",
                decision="invalid",
                error_type=error_code(error),
                detail=str(error),
            )
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
                    "transcript_failures": transcript_failures,
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
