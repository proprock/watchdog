"""One local polling process; durable desired state and no PID-based signals."""

import json
import math
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
from contextlib import ExitStack, closing, contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from agent_watchdog import daemon_lifecycle, resources
from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.capture import TICK_BUDGET, CaptureCursor, capture
from agent_watchdog.capture_store import CAPTURE_FILENAME, CaptureStore, write_manifest
from agent_watchdog.config import (
    Config,
    ConfigError,
    Limits,
    UserPaths,
    load_config,
    project_aliases,
    save_config,
)
from agent_watchdog.demotion import observe as observe_denial
from agent_watchdog.demotion import remember_denial
from agent_watchdog.diagnostics import Level, emit, error_code
from agent_watchdog.events import Envelope
from agent_watchdog.files import open_shared_read
from agent_watchdog.ignored import IgnoredRecords, IgnoreReason
from agent_watchdog.registry import Registry, RegistryError, Resolution
from agent_watchdog.rules.api import PROVISIONAL_PROFILES, Decision
from agent_watchdog.rules.engine import decide, subscriptions
from agent_watchdog.rules.python import PythonRuleTimeout
from agent_watchdog.scan import Scanner
from agent_watchdog.spool import SPOOL_BYTES, SPOOL_FILES, Spool
from agent_watchdog.state import SessionState, StateKey
from agent_watchdog.storage import (
    DrainResult,
    MaintenanceBudget,
    MaintenanceState,
    QuotaExceeded,
    RejectedEvent,
    StorageError,
    Store,
    WriterBusy,
    atomic_write,
    canonical,
    writer_lock,
)

CONTROL_REQUEST_BYTES = 8192
HEALTH_WINDOW_SECONDS = 60
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
    *,
    store: Store | None = None,
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
        with (
            nullcontext(store)
            if store is not None
            else Store(paths.project_data(project_id), project_id) as store
        ):
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
    except OSError, StorageError, ValueError:
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
        with open_shared_read(path) as stream:
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


def _warnings(report: Mapping[str, object]) -> list[str]:
    """Conditions that leave the daemon running but a capability silently unused (WD-155).

    Events a published subscription covers keep arriving while no adapter has ever
    asked the daemon about one: the installed adapter does not speak the channel's
    schema (or is not subscribed-aware), so every rule that needs a decision is inert.
    ``readiness`` names the two schemas.
    """
    channel = report.get("decision_channel")
    if (
        isinstance(channel, dict)
        and channel.get("subscribed_events_seen", 0) > 0
        and channel.get("requests", 0) == 0
    ):
        return ["decision_channel_unused"]
    return []


def status(paths: UserPaths) -> dict:
    control = desired(paths)
    running = alive(paths)
    now = time.time()
    reasons: list[str] = []
    try:
        report = (read_json(paths.runtime / "status.json") or {}) if running else {}
        if running:
            if "heartbeat" not in report:
                reasons.append("heartbeat_missing")
            else:
                stamp = float(report["heartbeat"])
                if not math.isfinite(stamp) or stamp > now:
                    reasons.append("heartbeat_invalid")
                elif now - stamp >= 10:
                    reasons.append("heartbeat_stale")
    except ValueError, OSError, TypeError:
        report = {}
        reasons.append("heartbeat_invalid")
    if report.get("errors"):
        reasons.append("project_or_configuration_error")
    startup = daemon_lifecycle.startup_evidence(paths.data, now=now)
    if startup.get("unavailable"):
        reasons.append("startup_evidence_unavailable")
    starts = startup.get("starts_without_heartbeat", 0)
    starts = starts if isinstance(starts, int) else 0
    if starts and not running:
        reasons.append("startup_failed")
    if starts >= 3:
        reasons.append("startup_crash_loop")
    health: dict[str, object] = {"startup": startup}
    # A stale hook adapter fills `invalid` losses while everything else looks healthy.
    mismatch = Spool(paths.data).schema_mismatch()
    if mismatch is not None:
        health["schema_mismatch"] = mismatch
        try:
            last = datetime.fromisoformat(mismatch["last_at"]).timestamp()
        except ValueError:
            last = now
        if now - last <= 24 * 3600:
            reasons.append("adapter_schema_mismatch")
    queues: dict[str, object] = {}
    try:
        loss_report = {"adapter": resources.losses(paths.data)}
        config = load_config(paths.config)
        for project in config.projects:
            loss_report[str(project.id)] = resources.losses(paths.project_data(project.id))
        queues = _queue_status(paths, config)
        stalled = _stalled_queues(queues, report.get("backlog", {}), now=now)
        health["stalled_queues"] = stalled
        if stalled:
            reasons.append("backlog_stalled")
        storage, lagging = _storage_health(paths, config, queues, now=now)
        health["storage"] = storage
        if storage["unavailable"]:
            reasons.append("storage_unavailable")
        if lagging:
            reasons.append("storage_lag")
    except OSError, ValueError:
        loss_report = {"unavailable": True}
        queues = {"unavailable": True}
        reasons.append("queue_or_configuration_unavailable")
    state = "running" if running else "unavailable"
    if reasons and (running or starts or "storage_lag" in reasons):
        state = "degraded"
    if control.get("paused", False):
        state = "paused"
    return report | {
        "state": state,
        "alive": running,
        "warnings": _warnings(report) if running else [],
        "reasons": reasons,
        "health": health,
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
    stats = []
    for path in entries:
        try:
            stats.append(path.stat())
        except FileNotFoundError:
            # A concurrent drain can remove a delivery during an on-demand snapshot.
            continue
    return {
        "files": len(stats),
        "bytes": sum(item.st_size for item in stats),
        "oldest_record_at": min((item.st_mtime for item in stats), default=None),
        "newest_record_at": max((item.st_mtime for item in stats), default=None),
        "file_limit": file_limit,
        "byte_limit": byte_limit,
    }


def _queue_counts(queues: dict) -> dict[str, int]:
    return {"spool": queues["spool"]["files"]}


class _BacklogTracker:
    """Track each queue independently so traffic elsewhere cannot hide a stall."""

    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}

    def observe(self, queues: dict, progressed: set[str], *, now: float) -> dict:
        pending = {}
        for key, count in _queue_counts(queues).items():
            if not count:
                continue
            previous = self.pending.get(key)
            if previous and key not in progressed and count >= previous["files"]:
                pending[key] = {**previous, "files": count, "polls": previous["polls"] + 1}
            else:
                pending[key] = {"files": count, "since": now, "polls": 1}
        self.pending = pending
        return pending


def _stalled_queues(queues: dict, backlog: dict, *, now: float) -> list[str]:
    if not isinstance(backlog, dict) or any(
        not isinstance(item, dict)
        or type(item.get("files")) is not int
        or type(item.get("polls")) is not int
        or type(item.get("since")) not in (float, int)
        or not math.isfinite(item["since"])
        for item in backlog.values()
    ):
        raise ValueError("Invalid backlog evidence")
    counts = _queue_counts(queues)
    return [
        key
        for key, item in backlog.items()
        if counts.get(key, 0) >= item["files"] > 0
        and item["polls"] >= 2
        and now - item["since"] >= HEALTH_WINDOW_SECONDS
    ]


def _storage_health(
    paths: UserPaths, config: Config, queues: dict, *, now: float
) -> tuple[dict[str, object], bool]:
    newest = None
    unavailable_projects: list[str] = []
    overall_deadline = time.monotonic() + 1.0
    for project in config.projects:
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            unavailable_projects.append(str(project.id))
            continue
        database = paths.project_data(project.id) / "events.sqlite3"
        if not database.exists():
            continue
        try:
            with closing(
                sqlite3.connect(
                    database.as_uri() + "?mode=ro", uri=True, timeout=min(0.1, remaining)
                )
            ) as db:
                # Bound a MAX scan even on a large store lacking a timestamp index.
                deadline = min(time.monotonic() + 0.1, overall_deadline)
                db.set_progress_handler(lambda end=deadline: int(time.monotonic() > end), 1000)
                row = db.execute("SELECT MAX(received_at) FROM events").fetchone()[0]
                if row:
                    stamp = datetime.fromisoformat(row).timestamp()
                    newest = max(newest, stamp) if newest is not None else stamp
        except OSError, sqlite3.Error, ValueError:
            unavailable_projects.append(str(project.id))
    unavailable = bool(unavailable_projects)
    spool = queues["spool"]
    latest = spool["newest_record_at"]
    oldest = spool["oldest_record_at"]
    lag = max(0, latest - newest) if latest is not None and newest is not None else None
    evidence = {
        "newest_stored_at": newest,
        "newest_spool_at": latest,
        "spool_timestamp_source": "filesystem_mtime",
        "lag_seconds": lag,
        "unavailable": unavailable,
        "unavailable_projects": unavailable_projects,
    }
    lagging = (
        not unavailable
        and oldest is not None
        and now - oldest >= HEALTH_WINDOW_SECONDS
        and (newest is None or (lag is not None and lag >= HEALTH_WINDOW_SECONDS))
    )
    return evidence, lagging


def _queue_status(paths: UserPaths, config: Config) -> dict[str, object]:
    return {
        "spool": _queue_snapshot(
            paths.data / "spool",
            byte_limit=SPOOL_BYTES,
            file_limit=SPOOL_FILES,
            skip="limits.json",
        ),
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
        allow_open_readers=True,
    )
    return request_id


def spool_payload_bytes(config: Config) -> int:
    """Largest per-project payload limit; the adapter caps stdin at this."""
    registered = max(
        (project.overrides.apply(config.defaults).payload_bytes for project in config.projects),
        default=config.defaults.payload_bytes,
    )
    return (
        max(registered, config.defaults.payload_bytes) if config.auto_add_projects else registered
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
    with suppress(ConfigError, OSError):
        write_spool_limits(paths, load_config(paths.config))
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
) -> tuple[Config, Resolution | IgnoreReason]:
    """Resolve a path against the latest config and persist a permitted auto-add."""
    with control_lock(paths, timeout=timeout):
        registry = Registry(load_config(paths.config))
        resolution, added = registry.resolve_or_auto_add(
            path,
            timeout=timeout,
            protected_roots=(paths.data, paths.runtime, paths.config.parent),
        )
        if added:
            save_config(paths.config, registry.config)
            _log(paths, registry.config, "INFO", event="registry", decision="auto_registered")
        return registry.config, resolution


def publish(
    paths: UserPaths, event: Envelope, *, config: Config | None = None, launch_daemon: bool = True
) -> bool:
    """Publish every Python-produced event to the same durable native spool."""

    with control_lock(paths, timeout=0.1):
        if desired(paths).get("paused", False):
            return False
        config = config or _config_cache.load(paths.config)
        project = next((item for item in config.projects if item.id == event.project_id), None)
        if project is None:
            return False
        limits = project.overrides.apply(config.defaults)
        try:
            Spool(
                paths.data, limits=limits, byte_limit=SPOOL_BYTES, file_limit=SPOOL_FILES
            ).publish(event, telemetry=config.pipeline_telemetry)
        except (QuotaExceeded, RejectedEvent, WriterBusy, OSError) as error:
            _log(
                paths,
                config,
                "WARNING",
                event="enqueue",
                decision="rejected",
                reason="quota" if isinstance(error, QuotaExceeded) else "invalid",
                error_type=error_code(error),
                project_id=project.id,
                event_id=event.event_id,
            )
            raise
    if launch_daemon and not alive(paths):
        launch(paths)
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


def _discard(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def _drain_spool(
    paths: UserPaths,
    config: Config,
    *,
    limit: int = 100,
    batch_size: int = 100,
    logged_unknown_checkouts: set[tuple[UUID, str]] | None = None,
    channel: _Channel | None = None,
    sessions: _SessionTracker | None = None,
    stores: dict[UUID, Store] | None = None,
    owner: ExitStack | None = None,
    maintenance_deadline: float | None = None,
    maintenance_states: dict[UUID, MaintenanceState] | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
    stats: DrainResult | None = None,
    spool: Spool | None = None,
    wake_projects: set[UUID] | None = None,
    ignored: IgnoredRecords | None = None,
) -> bool:
    """Resolve one queue window and commit bounded batches before acknowledging files."""
    from agent_watchdog.hooks import EVENTS, build_envelope, warn_unknown_fields
    from agent_watchdog.session_environment import (
        capture_checkout_outcome,
        capture_session_environment,
    )
    from agent_watchdog.storage import MaintenancePending, persisted_envelope

    if min(limit, batch_size) <= 0:
        raise StorageError("Invalid spool batch bounds")
    if owner is None:
        with ExitStack() as scope:
            return _drain_spool(
                paths,
                config,
                limit=limit,
                batch_size=batch_size,
                logged_unknown_checkouts=logged_unknown_checkouts,
                channel=channel,
                sessions=sessions,
                stores={},
                owner=scope,
                maintenance_deadline=maintenance_deadline,
                maintenance_states=maintenance_states,
                maintenance_budget=maintenance_budget,
                stats=stats,
                spool=spool,
                wake_projects=wake_projects,
                ignored=ignored,
            )
    assert stores is not None
    logged_unknown_checkouts = (
        logged_unknown_checkouts if logged_unknown_checkouts is not None else set()
    )
    queue = spool or Spool(paths.data)
    ignored = ignored if ignored is not None else IgnoredRecords(paths.data)
    queue.payload_bytes = spool_payload_bytes(config)
    result = stats if stats is not None else DrainResult()
    registry = Registry(config)
    resolved_checkouts: dict[str, Resolution] = {}
    pending: dict[UUID, list] = {}
    captured_checkouts: set[UUID] = set()
    activity = False
    registration_busy = False
    ignore_error: OSError | None = None
    try:
        ignored.recover()
    except OSError as error:
        ignore_error = error

    def invalid_record(error: Exception) -> None:
        _log(
            paths,
            config,
            "WARNING",
            event="spool",
            decision="retry" if isinstance(error, OSError) else "discarded",
            reason="invalid",
            error_type=error_code(error),
            detail=str(error),
        )

    for path, record in queue.records(result, limit=limit, on_error=invalid_record):
        if path in ignored:
            continue
        try:
            payload = None
            resolution = None
            cwd = None
            if record["record_type"] == "envelope":
                event = persisted_envelope(json.dumps(record["envelope"]))
                project = next(
                    (item for item in config.projects if item.id == event.project_id), None
                )
                if project is None:
                    _discard(path)
                    activity = True
                    continue
                limits = project.overrides.apply(config.defaults)
            else:
                provider, payload, cwd = record["provider"], record["input"], record["cwd"]
                if provider not in EVENTS or not isinstance(payload, dict):
                    raise RejectedEvent("Invalid provider input")
                if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                    raise RejectedEvent("Invalid spool cwd")
                event_id = UUID(str(record["event_id"]))
                received_at = datetime.fromisoformat(record["received_at"])
                if received_at.tzinfo is None:
                    raise RejectedEvent("Spool timestamp must have a timezone")
                target: Resolution | IgnoreReason | None = resolved_checkouts.get(cwd)
                try:
                    if target is None:
                        target = registry.resolve(Path(cwd), timeout=0.25)
                    if target is None:
                        if registration_busy:
                            continue
                        config, target = resolve_or_auto_register(paths, Path(cwd), timeout=0.25)
                        registry = Registry(config)
                        resolved_checkouts.clear()
                except RegistryError:
                    target = "unresolvable_cwd"
                except (ConfigError, WriterBusy) as error:
                    registration_busy = isinstance(error, WriterBusy)
                    _log(
                        paths,
                        config,
                        "DEBUG",
                        event="spool",
                        decision="deferred",
                        reason="auto_registration",
                        error_type=error_code(error),
                    )
                    continue
                if not isinstance(target, Resolution):
                    directory = os.path.normpath(cwd)
                    try:
                        warn = ignored.record(path, directory, target)
                    except OSError as error:
                        ignore_error = error
                        continue
                    if warn:
                        _log(
                            paths,
                            config,
                            "WARNING",
                            event="spool",
                            decision="ignored",
                            reason=target,
                            detail=directory,
                        )
                    activity = True
                    continue
                resolution = target
                resolved_checkouts[cwd] = resolution
                project = next(item for item in config.projects if item.id == resolution.project_id)
                limits = project.overrides.apply(config.defaults)
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

            delivery = record.get("delivery", {})
            if not isinstance(delivery, dict):
                raise RejectedEvent("Invalid spool delivery telemetry")
            control = delivery.get("control")
            control_fields = {}
            if isinstance(control, dict):
                received, rendered = (
                    control.get("response_received"),
                    control.get("adapter_rendered"),
                )
                if type(received) is bool and (rendered is None or type(rendered) is bool):
                    bounded = {"response_received": received, "adapter_rendered": rendered}
                    action = control.get("requested_action")
                    if action in {"allow", "deny", "ask", "rewrite", "context", "block", "permit"}:
                        bounded["requested_action"] = action
                    control_fields["control"] = bounded
            if config.pipeline_telemetry:
                observed_at = datetime.now(UTC).isoformat()
                trace = {key: value for key, value in delivery.items() if key != "control"}
                trace |= {"spool_drained_at": observed_at, "sqlite_write_started_at": observed_at}
            else:
                trace = {}
            event = event.model_copy(update={"delivery": event.delivery | trace | control_fields})
            if not limits.capture_content and event.source == "hook":
                namespace = event.payload.get(event.provider)
                if isinstance(namespace, dict):
                    event = event.model_copy(
                        update={
                            "payload": {event.provider: namespace | {"content": "omitted"}},
                            "availability": event.availability | {"content": "unavailable"},
                        }
                    )
            if len(canonical(event).encode()) > limits.payload_bytes:
                resources.count_loss(paths.project_data(project.id), "payload")
                _log(
                    paths,
                    config,
                    "WARNING",
                    event="spool",
                    decision="discarded",
                    reason="oversized",
                    project_id=project.id,
                    event_id=event.event_id,
                )
                _discard(path)
                activity = True
                continue
            pending.setdefault(project.id, []).append((path, event, payload, resolution, cwd))
        except (ValidationError, RejectedEvent, ValueError, KeyError, TypeError) as error:
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
            queue.quarantine(path, result)
            activity = True

    projects = {project.id: project for project in config.projects}
    for project_id, items in pending.items():
        project = projects.get(project_id)
        if project is None:
            continue
        limits = project.overrides.apply(config.defaults)
        try:
            store = stores.get(project_id)
            if store is None:
                store = owner.enter_context(
                    Store(paths.project_data(project_id), project_id, limits=limits)
                )
                stores[project_id] = store
                if maintenance_states is not None and project_id in maintenance_states:
                    store.maintenance_state = maintenance_states[project_id]
            store.maintenance_deadline = maintenance_deadline
            store.maintenance_budget = maintenance_budget
            if maintenance_states is not None:
                maintenance_states[project_id] = store.maintenance_state
            capture_store = CaptureStore(paths.project_data(project_id) / CAPTURE_FILENAME)
            for start in range(0, len(items), batch_size):
                chunk = items[start : start + batch_size]
                lookup = {
                    id(event): (payload, resolution, cwd)
                    for _, event, payload, resolution, cwd in items
                }

                def absorb(_store=store, _project_id=project_id) -> None:
                    if sessions is not None:
                        sessions.absorb(str(_project_id), _store.touched)

                def committed(
                    event: Envelope,
                    _lookup=lookup,
                    _project_id=project_id,
                    _store=store,
                    _captured=captured_checkouts,
                    _capture_store=capture_store,
                    _limits=limits,
                ) -> None:
                    if wake_projects is not None:
                        wake_projects.add(_project_id)
                    payload, resolution, cwd = _lookup[id(event)]
                    demoted = observe_denial(paths, event)
                    if demoted is not None:
                        _log(
                            paths,
                            config,
                            "WARNING",
                            event="control",
                            decision="profile_demoted",
                            detail=demoted,
                        )
                    if payload is not None and channel is not None:
                        channel.observe(event.provider, payload, event.received_at)
                    if (
                        resolution is not None
                        and event.checkout_id is not None
                        and event.checkout_id not in _captured
                    ):
                        _captured.add(event.checkout_id)
                        _capture_diff_snapshot(
                            event.checkout_id,
                            resolution.root,
                            _project_id,
                            paths,
                            config,
                            logged_unknown_checkouts,
                            store=_store,
                        )
                    if payload is not None:
                        checkout = resolution.root if resolution is not None else None
                        try:
                            capture_session_environment(
                                _capture_store,
                                event,
                                payload=payload,
                                cwd=cwd,
                                checkout=checkout,
                                paths=paths,
                                config=config,
                                limits=_limits,
                            )
                        except (
                            OSError,
                            sqlite3.Error,
                            StorageError,
                            ValueError,
                            RuntimeError,
                        ) as error:
                            _log(
                                paths,
                                config,
                                "WARNING",
                                event="session_environment",
                                decision="unavailable",
                                project_id=_project_id,
                                error_type=error_code(error),
                            )
                        try:
                            capture_checkout_outcome(
                                _capture_store,
                                event,
                                trigger=payload.get("hook_event_name"),
                                checkout=checkout,
                                limits=_limits,
                            )
                        except (
                            OSError,
                            sqlite3.Error,
                            StorageError,
                            ValueError,
                            RuntimeError,
                        ) as error:
                            _log(
                                paths,
                                config,
                                "WARNING",
                                event="checkout_outcome",
                                decision="unavailable",
                                project_id=_project_id,
                                error_type=error_code(error),
                            )
                    _log(
                        paths,
                        config,
                        "DEBUG",
                        event="spool",
                        decision="admitted",
                        project_id=_project_id,
                        event_id=event.event_id,
                    )

                with control_lock(paths, timeout=0.1):
                    if desired(paths).get("paused", False):
                        return activity
                queue.commit(
                    store,
                    [(path, event) for path, event, _, _, _ in chunk],
                    result,
                    on_commit=committed,
                    after_commit=absorb,
                )
        except MaintenancePending:
            _log(
                paths,
                config,
                "DEBUG",
                event="spool",
                decision="deferred",
                reason="maintenance",
                project_id=project_id,
            )
        except WriterBusy:
            _log(
                paths,
                config,
                "DEBUG",
                event="spool",
                decision="retry",
                reason="writer_busy",
                project_id=project_id,
            )
        except QuotaExceeded:
            if store is None:
                for path, _, _, _, _ in items:
                    resources.count_loss(paths.project_data(project_id), "quota")
                    _discard(path)
                activity = True
                continue
            # A later input may be an already committed replay and need no space.
            # Revisit remaining records separately rather than dropping a whole window.
            for path, remaining_event, _, _, _ in items:
                if not path.exists():
                    continue
                try:
                    queue.commit(
                        store,
                        [(path, remaining_event)],
                        result,
                        on_commit=committed,
                        after_commit=absorb,
                    )
                except MaintenancePending:
                    break
                except QuotaExceeded:
                    resources.count_loss(paths.project_data(project_id), "quota")
                    _discard(path)
                    activity = True
                except OSError, StorageError, sqlite3.Error:
                    break
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="discarded",
                reason="quota",
                project_id=project_id,
            )
        except (OSError, StorageError, sqlite3.Error) as error:
            _log(
                paths,
                config,
                "WARNING",
                event="spool",
                decision="retry",
                error_type=error_code(error),
                detail=str(error),
                project_id=project_id,
            )
    flushed_ignored = False
    try:
        flushed_ignored = ignored.flush()
    except OSError as error:
        ignore_error = error
    if ignore_error is not None:
        _log(
            paths,
            config,
            "WARNING",
            event="spool",
            decision="retry",
            reason="ignored_counters",
            error_type=error_code(ignore_error),
            detail=str(ignore_error),
        )
    return (
        activity
        or flushed_ignored
        or bool(result.inserted + result.duplicates + result.quarantined + result.discarded)
    )


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
        return _request_budget(load_config(paths.config))
    except ConfigError, OSError:
        return Limits().payload_bytes + _POLICY_REQUEST_OVERHEAD_BYTES


def _request_budget(config: Config) -> int:
    return spool_payload_bytes(config) + _POLICY_REQUEST_OVERHEAD_BYTES


def _record_control(
    paths: UserPaths,
    provider: str,
    hook_input: Mapping[str, object],
    event_id: UUID,
    decision: Decision,
    *,
    selected_at: str | None = None,
    response_sent_at: str | None = None,
) -> None:
    """Record a selected action and whether its socket response was sent.

    Runs on a handler thread, so it never opens a ``Store`` (the poll thread
    holds the writer lock); publication uses the same spool as provider hooks.
    The daemon cannot see whether the adapter rendered the
    answer in time, so "delivered" means "sent". Recording failures are logged
    after the response attempt and never change the adapter's decision.
    """
    if decision.action == "allow" or decision.project_id is None:
        return
    session_id = hook_input.get("session_id")
    sent_at = None if decision.capability_gap else response_sent_at
    tool_use_id = hook_input.get("tool_use_id")
    if (
        provider == "codex"
        and decision.action == "deny"
        and sent_at is not None
        and decision.codex_control_profile is not None
        and decision.codex_control_profile in PROVISIONAL_PROFILES
        and isinstance(session_id, str)
        and isinstance(tool_use_id, str)
        and session_id
        and tool_use_id
    ):
        remember_denial(session_id, tool_use_id, decision.codex_control_profile, decision.reason)
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
                    # The selected text; send timestamps say whether the response was sent.
                    "text": decision.reason or decision.context,
                    "evidence_ids": [str(event_id)],
                    # Historical delivery means socket send, never provider effect.
                    "delivered_at": sent_at,
                    "selected_at": selected_at,
                    "response_sent_at": sent_at,
                    "adapter_rendered": None,
                    "provider_effect": None,
                    **(
                        {
                            "capability_gap": decision.capability_gap,
                            "requested_action": decision.requested_action,
                        }
                        if decision.capability_gap
                        else {}
                    ),
                    **(
                        {"codex_control_profile": decision.codex_control_profile}
                        if decision.codex_control_profile
                        else {}
                    ),
                }
            },
        )
        publish(paths, envelope, config=config, launch_daemon=False)
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

    server: _PolicyServer

    @staticmethod
    def _session(
        server: _PolicyServer, provider: str, hook_input: Mapping[str, object]
    ) -> SessionState | None:
        """The daemon's in-memory state of the calling agent; None until it is observed."""
        session_id, agent_id = hook_input.get("session_id"), hook_input.get("agent_id")
        if server.watchdog_sessions is None or not isinstance(session_id, str):
            return None
        return server.watchdog_sessions.session(
            provider, session_id, agent_id if isinstance(agent_id, str) else ""
        )

    @staticmethod
    def _rule_failed(server: _PolicyServer, name: str, error: Exception) -> None:
        """Log errors and the first disabling timeout independently, once per lifetime."""
        key = f"{name}:timeout" if isinstance(error, PythonRuleTimeout) else name
        if key in server.watchdog_failed_rules:
            return
        server.watchdog_failed_rules.add(key)
        _log(
            server.watchdog_paths,
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
        self.answer(self.server, line, self._send_response)

    def _send_response(self, payload: bytes) -> None:
        self.wfile.write(payload)
        # Recording the control event must never add to the adapter's wait.
        self.connection.shutdown(socket.SHUT_WR)

    @staticmethod
    def answer(server: _PolicyServer, line: bytes, send_response: Callable[[bytes], None]) -> None:
        try:
            request = json.loads(line)
        except UnicodeDecodeError, json.JSONDecodeError:
            return
        if not isinstance(request, dict) or request.get("schema_version") != _POLICY_SCHEMA_VERSION:
            return
        if not secrets.compare_digest(str(request.get("token", "")), server.watchdog_token):
            return
        provider, hook_input = request.get("provider"), request.get("input")
        if provider not in {"claude", "codex"} or not isinstance(hook_input, dict):
            return
        try:
            event_id = UUID(str(request.get("event_id")))
        except ValueError:
            return
        server.watchdog_stats.request()
        paths = server.watchdog_paths
        try:
            config = _config_cache.load(paths.config)
            decision = decide(
                paths,
                config,
                str(provider),
                hook_input,
                session=_PolicyRequestHandler._session(server, str(provider), hook_input),
                on_error=lambda name, error: _PolicyRequestHandler._rule_failed(
                    server, name, error
                ),
                codex_control_profile=request.get("codex_control_profile"),
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
        selected_at = datetime.now(UTC).isoformat()
        response_sent_at = None
        try:
            send_response(json.dumps(_decision_response(decision)).encode())
            response_sent_at = datetime.now(UTC).isoformat()
        except OSError:
            pass  # Selection is still observable when the adapter disconnects.
        _record_control(
            paths,
            str(provider),
            hook_input,
            event_id,
            decision,
            selected_at=selected_at,
            response_sent_at=response_sent_at,
        )


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
    watchdog_sessions: _SessionTracker | None
    watchdog_failed_rules: set[str]
    watchdog_stats: _ChannelStats


class _ChannelStats:
    """What `daemon status` needs to tell an adapter that never asks from an idle channel.

    ``requests`` counts well-formed, authenticated decision requests; ``subscribed_seen``
    counts events drained from the spool during this daemon's lifetime that a published
    subscription covers, which an adapter speaking the channel would have asked about.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self._lock = threading.Lock()
        self._requests = 0
        self._subscribed_seen = 0

    def request(self) -> None:
        with self._lock:
            self._requests += 1

    def subscribed_event(self) -> None:
        with self._lock:
            self._subscribed_seen += 1

    def snapshot(self) -> dict[str, int | str]:
        """The counts plus the window they cover, since a count of zero means little without it."""
        with self._lock:
            return {
                "observed_since": self.started_at.isoformat(),
                "requests": self._requests,
                "subscribed_events_seen": self._subscribed_seen,
            }


class _Channel:
    """The published discovery file, kept in step with the rules that may run.

    ``sync`` runs on every poll tick: it rewrites ``socket.json`` only when the
    subscriptions changed, so approving, editing or disabling a rule takes
    effect without a restart. A failed rewrite keeps the previous file, which
    can only subscribe to more than is needed (the engine answers ``allow``
    for it) and never to something an adapter would act on without a rule.
    """

    def __init__(
        self,
        paths: UserPaths,
        document: dict[str, object] | None = None,
        stats: _ChannelStats | None = None,
        server: _PolicyServer | None = None,
    ) -> None:
        self._paths = paths
        self._document = document
        self._server = server
        self._failing = False
        self.stats = stats if stats is not None else _ChannelStats()

    def observe(
        self, provider: str, hook_input: Mapping[str, object], received_at: datetime
    ) -> None:
        """Count a freshly drained event that a published subscription covers."""
        if self._document is None or received_at < self.stats.started_at:
            return
        event, tool = hook_input.get("hook_event_name"), hook_input.get("tool_name")
        entries = self._document.get("subscriptions")
        for entry in entries if isinstance(entries, list) else []:
            if (
                entry["provider"] == provider
                and entry["hook_event_name"] == event
                and entry["tool_name"] in (None, tool)
            ):
                self.stats.subscribed_event()
                return

    def sync(self, config: Config) -> None:
        if self._document is None:
            return
        try:
            wanted = subscriptions(self._paths, config)
            budget = _request_budget(config)
            if wanted == self._document["subscriptions"] and budget == self._document.get(
                "max_request_bytes"
            ):
                self._failing = False
                return
            document = {**self._document, "subscriptions": wanted, "max_request_bytes": budget}
            server = self._server
            raising = server is not None and budget > server.watchdog_request_bytes
            # The server must never read less than the file promises: raise it
            # before publishing, lower it only after the file is published.
            if raising and server is not None:
                server.watchdog_request_bytes = budget
            atomic_write(self._paths.data / "policy" / "socket.json", json.dumps(document).encode())
            if server is not None:
                server.watchdog_request_bytes = budget
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
    except ConfigError, OSError:
        return []


@contextmanager
def _policy_server(paths: UserPaths, sessions: _SessionTracker | None = None) -> Iterator[_Channel]:
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
    stats = _ChannelStats()
    server.watchdog_stats = stats
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
        with suppress(OSError):
            socket_path.unlink(missing_ok=True)
        yield _Channel(paths)
        return
    try:
        with ExitStack() as benchmark:
            if os.environ.get("WATCHDOG_BENCHMARK_HTTP") == "1":
                from agent_watchdog.benchmark_http import benchmark_http_server

                try:
                    benchmark.enter_context(
                        benchmark_http_server(
                            socket_path,
                            document,
                            lambda line, send: _PolicyRequestHandler.answer(server, line, send),
                        )
                    )
                except OSError as error:
                    _log(
                        paths,
                        None,
                        "WARNING",
                        event="lifecycle",
                        decision="degraded",
                        error_type=error_code(error),
                    )
            yield _Channel(paths, document, stats, server)
    finally:
        with suppress(OSError):
            socket_path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run(paths: UserPaths) -> int:
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as owner:
            owner.enter_context(writer_lock(paths.data / "daemon.lock"))
            daemon_lifecycle.record_start(paths.data, now=time.time())
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


def _machine_id(data: Path) -> UUID:
    path = data / "machine_id"
    try:
        identity = UUID(path.read_text(encoding="utf-8").strip())
        if identity.version != 4:
            raise ValueError("Expected UUID4")
    except FileNotFoundError:
        identity = uuid4()
        atomic_write(path, f"{identity}\n".encode())
    except ValueError as error:
        raise StorageError("Invalid machine_id; restore the original UUID4 file") from error
    return identity


def _poll(
    paths: UserPaths,
    owner: ExitStack,
    sessions: _SessionTracker | None = None,
    channel: _Channel | None = None,
) -> None:
    machine_id = _machine_id(paths.data)
    sessions = sessions or _SessionTracker()
    channel = channel or _Channel(paths)
    logged_findings_failures: set[tuple[str, str]] = set()
    instance_id = str(uuid4())
    delay = 0.25
    next_visit: dict[UUID, float] = {}
    visit_intervals: dict[UUID, float] = {}
    project_errors: set[str] = set()
    project_failures: dict[str, list[str]] = {}
    maintenance: dict[str, float] = {}
    maintenance_states: dict[UUID, MaintenanceState] = {}
    capture_cursors: dict[UUID, CaptureCursor] = {}
    maintenance_cursor = 0
    spool_maintenance_at = 0.0
    spool_queue = Spool(paths.data)
    ignored = IgnoredRecords(paths.data)
    scanner = Scanner(paths)
    logged_enrichment_failures: set[tuple[str, str]] = set()
    logged_inert_enrichment: set[str] = set()
    logged_unknown_checkouts: set[tuple[UUID, str]] = set()
    spool_mtime: float | None = None
    backlog = _BacklogTracker()
    progressed: set[str] = set()
    pending: dict = {}
    backlog_sample_at = 0.0
    first_heartbeat = True
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
        capture_budget = TICK_BUDGET
        activity = False
        tick_owner = ExitStack()
        stores: dict[UUID, Store] = {}
        maintenance_budget = MaintenanceBudget(remaining=0.2)
        try:
            config = _config_cache.load(paths.config)
            scan_reserve = TICK_BUDGET / 2 if scanner.due(config.scan, time.monotonic()) else 0.0
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
            if _drain_controls(paths, config):
                activity = True
            aliases = project_aliases(config.projects)
            projects = [*config.projects, None]
            offset = maintenance_cursor % len(projects)
            projects = projects[offset:] + projects[:offset]
            maintenance_cursor = (offset + 1) % len(projects)
            for project in projects:
                if project is None:
                    if time.monotonic() >= spool_maintenance_at:
                        unfinished = spool_queue.maintain(maintenance_budget)
                        spool_maintenance_at = 0 if unfinished else time.monotonic() + 60
                    continue
                project_key = str(project.id)
                if time.monotonic() < next_visit.get(project.id, 0):
                    if project_key in project_errors and len(errors) < 32:
                        errors.append(project_key)
                    if project_key in project_failures:
                        transcript_failures[project_key] = project_failures[project_key]
                    continue
                project_active = False
                try:
                    root = paths.project_data(project.id)
                    limits = project.overrides.apply(config.defaults)
                    store = stores.get(project.id)
                    if store is None:
                        store = tick_owner.enter_context(Store(root, project.id, limits=limits))
                        stores[project.id] = store
                        if project.id in maintenance_states:
                            store.maintenance_state = maintenance_states[project.id]
                    store.maintenance_budget = maintenance_budget
                    maintenance_states[project.id] = store.maintenance_state
                    with nullcontext(store):
                        maintained = time.monotonic() >= maintenance.get(str(project.id), 0)
                        if maintained:
                            maintained_result = store.maintain()
                            project_active |= maintained_result.pending
                            activity |= maintained_result.pending
                            maintenance[str(project.id)] = (
                                0 if maintained_result.pending else time.monotonic() + 60
                            )
                        if maintained or not sessions.knows(str(project.id)):
                            sessions.load(str(project.id), store)
                        try:
                            from agent_watchdog.transcripts import enrich, failure_code

                            enrichment = enrich(
                                store,
                                active_failure_since=datetime.now(UTC)
                                - timedelta(minutes=limits.transcript_failure_minutes),
                            )
                            project_active |= bool(enrichment) or enrichment.progressed
                            activity |= bool(enrichment) or enrichment.progressed
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
                        if limits.capture_content and capture_budget > scan_reserve:
                            capture_started = time.monotonic()
                            try:
                                captured = capture(
                                    store,
                                    clock=time.monotonic,
                                    budget_seconds=capture_budget - scan_reserve,
                                    cursor=capture_cursors.setdefault(project.id, CaptureCursor()),
                                )
                            finally:
                                capture_budget -= time.monotonic() - capture_started
                            project_active |= captured.progressed or captured.pending
                            activity |= captured.progressed or captured.pending
                            if captured.degraded:
                                if project_key not in project_errors:
                                    _log(
                                        paths,
                                        config,
                                        "WARNING",
                                        event="capture",
                                        decision="degraded",
                                        project_id=project.id,
                                    )
                                if project_key not in errors and len(errors) < 32:
                                    errors.append(project_key)
                        sessions.absorb(str(project.id), store.touched)
                        refreshed, failures = sessions.refresh(
                            str(project.id), store, now=time.monotonic()
                        )
                        project_active |= bool(refreshed)
                        activity |= bool(refreshed)
                        _record_findings_failures(
                            paths, config, project.id, failures, logged_findings_failures
                        )
                        if resources.available(root, limits) < 65536 and len(errors) < 32:
                            errors.append(str(project.id))
                    write_manifest(
                        root, machine_id=machine_id, project=project, name=aliases[project.id]
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
                if project_key in errors:
                    project_errors.add(project_key)
                else:
                    project_errors.discard(project_key)
                if project_key in transcript_failures:
                    project_failures[project_key] = transcript_failures[project_key]
                else:
                    project_failures.pop(project_key, None)
                if project_active:
                    visit_intervals[project.id] = 1.0
                    next_visit[project.id] = time.monotonic()
                else:
                    interval = min(60.0, visit_intervals.get(project.id, 0.5) * 2)
                    visit_intervals[project.id] = interval
                    next_visit[project.id] = time.monotonic() + interval
            wake_projects: set[UUID] = set()
            if _drain_spool(
                paths,
                config,
                logged_unknown_checkouts=logged_unknown_checkouts,
                channel=channel,
                sessions=sessions,
                stores=stores,
                owner=tick_owner,
                maintenance_states=maintenance_states,
                maintenance_budget=maintenance_budget,
                spool=spool_queue,
                wake_projects=wake_projects,
                ignored=ignored,
            ):
                activity = True
                progressed.add("spool")
            for project_id in wake_projects:
                next_visit[project_id] = time.monotonic()
                visit_intervals[project_id] = 1.0
            if scanner.tick(
                config, resolve_or_auto_register, ignored, capture_budget=capture_budget
            ):
                activity = True
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
        except (OSError, StorageError, sqlite3.Error) as error:
            errors.append("pipeline")
            _log(
                paths,
                config,
                "WARNING",
                event="poll",
                decision="degraded",
                error_type=error_code(error),
                detail=str(error),
            )
        finally:
            tick_owner.close()
        if time.monotonic() >= backlog_sample_at:
            try:
                pending = (
                    backlog.observe(_queue_status(paths, config), progressed, now=time.time())
                    if config is not None
                    else {}
                )
            except OSError:
                pending = {}
            progressed.clear()
            backlog_sample_at = time.monotonic() + 10
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
                    "decision_channel": channel.stats.snapshot(),
                    "backlog": pending,
                }
            ).encode(),
        )
        if first_heartbeat:
            daemon_lifecycle.record_ready(paths.data)
            first_heartbeat = False
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
