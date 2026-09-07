"""Best-effort, content-free operational logging for the Python core."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from agent_watchdog.config import Limits
from agent_watchdog.storage import WriterBusy, writer_lock

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_RANK: dict[Level, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_FIELD_NAMES = {
    "component",
    "event",
    "decision",
    "reason",
    "error_type",
    "project_id",
    "event_id",
    "count",
    "bytes",
}
_COMPONENTS = {"daemon", "hook", "cli"}
_EVENTS = {
    "command",
    "configuration",
    "control",
    "enqueue",
    "enrichment",
    "lifecycle",
    "observe",
    "poll",
    "project",
    "registry",
    "spool",
}
_DECISIONS = {
    "active",
    "admitted",
    "already_running",
    "auto_registered",
    "deferred",
    "degraded",
    "disabled",
    "discarded",
    "failed",
    "idle",
    "interrupted",
    "invalid",
    "paused",
    "received",
    "rejected",
    "reloaded",
    "retry",
    "started",
    "stopped",
    "unregistered",
}
_REASONS = {"invalid", "linked", "oversized", "quota", "writer_busy"}
_ERROR_TYPES = {
    "keyerror",
    "operationalerror",
    "oserror",
    "registryerror",
    "rejectedevent",
    "storageerror",
    "typeerror",
    "unexpected",
    "validationerror",
    "valueerror",
}


def error_code(error: BaseException) -> str:
    """Return a fixed error category without retaining exception text."""
    name = type(error).__name__.lower()
    return name if name in _ERROR_TYPES else "unexpected"


def emit(
    data: Path,
    limits: Limits,
    level: Level,
    *,
    component: str,
    event: str,
    decision: str | None = None,
    reason: str | None = None,
    error_type: str | None = None,
    project_id: UUID | None = None,
    event_id: UUID | None = None,
    count: int | None = None,
    bytes: int | None = None,
) -> None:
    """Append one allowlisted record without delaying or exposing core work.

    The interface intentionally accepts no free-form data.  All strings are fixed
    implementation codes, while IDs are Watchdog UUIDs created or persisted here.
    """
    if _RANK[level] < _RANK[limits.log_level]:
        return
    fields: dict[str, str | int] = {"component": component, "event": event}
    for name, value in {
        "decision": decision,
        "reason": reason,
        "error_type": error_type,
    }.items():
        if value is not None:
            fields[name] = value
    for name, value in {"project_id": project_id, "event_id": event_id}.items():
        if value is not None:
            fields[name] = str(value)
    for name, value in {"count": count, "bytes": bytes}.items():
        if value is not None:
            fields[name] = value
    if not _valid(fields):
        return
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    line = (
        " ".join(
            [f"timestamp={stamp}", f"level={level}"]
            + [f"{name}={value}" for name, value in fields.items()]
        )
        + "\n"
    )
    _append(data, limits, line.encode("ascii"))


def _valid(fields: dict[str, str | int]) -> bool:
    if set(fields) - _FIELD_NAMES:
        return False
    if fields["component"] not in _COMPONENTS or fields["event"] not in _EVENTS:
        return False
    if "decision" in fields and fields["decision"] not in _DECISIONS:
        return False
    if "reason" in fields and fields["reason"] not in _REASONS:
        return False
    if "error_type" in fields and fields["error_type"] not in _ERROR_TYPES:
        return False
    for name in ("count", "bytes"):
        value = fields.get(name)
        if value is not None and (not isinstance(value, int) or value < 0):
            return False
    return True


def _regular(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        return not path.exists() or path.is_file()
    except OSError:
        return False


def _append(data: Path, limits: Limits, line: bytes) -> None:
    """Serialize append/rotation across daemon and short-lived CLI processes."""
    try:
        if data.is_symlink():
            return
        data.mkdir(parents=True, exist_ok=True)
        with writer_lock(data / "watchdog.log.lock"):
            active = data / "watchdog.log"
            if not _regular(active):
                return
            size = active.stat().st_size if active.exists() else 0
            if size and size + len(line) > limits.log_bytes:
                _rotate(active, limits.log_files)
            with active.open("ab", buffering=0) as stream:
                stream.write(line)
    except (OSError, WriterBusy):
        return


def _rotate(active: Path, files: int) -> None:
    if files == 1:
        active.unlink(missing_ok=True)
        return
    oldest = active.with_name(f"{active.name}.{files - 1}")
    if oldest.exists():
        if not _regular(oldest):
            raise OSError("Unsafe rotated log")
        oldest.unlink()
    for index in range(files - 2, 0, -1):
        source = active.with_name(f"{active.name}.{index}")
        if source.exists():
            if not _regular(source):
                raise OSError("Unsafe rotated log")
            source.replace(active.with_name(f"{active.name}.{index + 1}"))
    active.replace(active.with_name(f"{active.name}.1"))
