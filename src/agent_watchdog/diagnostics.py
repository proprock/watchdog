"""Best-effort operational logging for the Python core.

Records are content-free by default: fixed codes, bounded field names, and local
Watchdog UUIDs only. Setting ``[defaults] log_detail = true`` additionally appends
one ``detail="..."`` clause carrying a de-identified, length-bounded error string
(known credential forms removed, paths kept). It is opt-in so the shipped default,
exports, and shared bug reports stay content-free.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from agent_watchdog.config import Limits
from agent_watchdog.privacy import text as _redact_text
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
    "provider",
    "field",
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
    "acknowledged",
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
    "unavailable",
    "unregistered",
}
_REASONS = {"invalid", "linked", "oversized", "quota", "unknown_field", "writer_busy"}
_PROVIDERS = {"codex", "claude"}
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
# An error category is a short, content-free code: either an exception class name
# or an internal failure Literal. Its shape is bounded here, like ``field``; the
# value never carries exception text, paths, or payload data.
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_DETAIL_MAX = 200
_DETAIL_WS = re.compile(r"\s+")


def _detail(value: str) -> str:
    """Bound and de-identify a free-text error string for opt-in logging.

    Known credential forms are removed; whitespace is collapsed to keep the log's
    ``key=value`` grammar; non-printable and non-ASCII characters are replaced; the
    result is truncated. Paths and non-credential payload fragments are retained on
    purpose - that is the point of enabling ``log_detail``.
    """
    value = _DETAIL_WS.sub(" ", _redact_text(value)).strip().replace('"', "'")
    value = "".join(ch if " " <= ch <= "~" else "?" for ch in value)
    if len(value) > _DETAIL_MAX:
        value = value[:_DETAIL_MAX].rstrip() + "..."
    return value


def error_code(error: BaseException) -> str:
    """Return the exception class name as a content-free category.

    Class names are source identifiers, never user data, so keeping the real name
    (instead of collapsing unknown types to ``unexpected``) gives an investigation
    a starting point without exposing the exception message.
    """
    name = type(error).__name__.lower()
    return name if _ERROR_TYPE.fullmatch(name) else "unexpected"


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
    provider: str | None = None,
    field: str | None = None,
    detail: str | None = None,
) -> None:
    """Append one record without delaying or exposing core work.

    ``component``, ``event``, ``decision``, ``reason`` and ``provider`` are closed
    allowlists; ``error_type`` and ``field`` are shape-bounded identifiers (an
    exception class name, an internal Literal, or an observed field name, never
    exception text or a path); IDs are Watchdog UUIDs created or persisted here.

    ``detail`` is free text (typically ``str(error)``). It is dropped unless
    ``limits.log_detail`` is set, and even then it is de-identified and truncated
    by ``_detail`` before it is appended as a trailing ``detail="..."`` clause.
    """
    if _RANK[level] < _RANK[limits.log_level]:
        return
    fields: dict[str, str | int] = {"component": component, "event": event}
    for name, value in {
        "decision": decision,
        "reason": reason,
        "error_type": error_type,
        "provider": provider,
        "field": field,
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
    parts = [f"timestamp={stamp}", f"level={level}"] + [
        f"{name}={value}" for name, value in fields.items()
    ]
    if detail is not None and limits.log_detail:
        cleaned = _detail(detail)
        if cleaned:
            parts.append(f'detail="{cleaned}"')
    line = " ".join(parts) + "\n"
    _append(data, limits, line.encode("ascii", "replace"))


def _valid(fields: dict[str, str | int]) -> bool:
    if set(fields) - _FIELD_NAMES:
        return False
    if fields["component"] not in _COMPONENTS or fields["event"] not in _EVENTS:
        return False
    if "decision" in fields and fields["decision"] not in _DECISIONS:
        return False
    if "reason" in fields and fields["reason"] not in _REASONS:
        return False
    if "error_type" in fields and (
        not isinstance(fields["error_type"], str)
        or _ERROR_TYPE.fullmatch(fields["error_type"]) is None
    ):
        return False
    if "provider" in fields and fields["provider"] not in _PROVIDERS:
        return False
    if "field" in fields and (
        not isinstance(fields["field"], str) or _FIELD.fullmatch(fields["field"]) is None
    ):
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
