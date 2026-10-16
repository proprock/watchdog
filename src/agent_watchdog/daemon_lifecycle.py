"""Bounded durable evidence of starts that did not publish their first heartbeat."""

import json
import math
from pathlib import Path

from agent_watchdog.storage import atomic_write

_WINDOW_SECONDS = 60
_MAX_STARTS = 32
_MAX_BYTES = 8192
_EVIDENCE_ERRORS = (OSError, ValueError, TypeError, OverflowError, RecursionError)


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid startup timestamp")
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ValueError("Invalid startup timestamp")
    return timestamp


def _read(data: Path) -> tuple[list[float], float | None]:
    try:
        with (data / "daemon-health.json").open("rb") as stream:
            content = stream.read(_MAX_BYTES)
    except FileNotFoundError:
        return [], None
    # Our own history is far smaller. Reject a full buffer without reading beyond the bound.
    if len(content) == _MAX_BYTES:
        raise ValueError("Startup evidence exceeds limit")
    document = json.loads(content)
    if not isinstance(document, dict):
        raise ValueError("Invalid startup evidence")
    pending = document.get("pending_starts")
    if not isinstance(pending, list) or len(pending) > _MAX_STARTS:
        raise ValueError("Invalid startup history")
    if "last_started_at" not in document:
        raise ValueError("Missing startup timestamp")
    last = document["last_started_at"]
    return [_timestamp(value) for value in pending], None if last is None else _timestamp(last)


def _write(data: Path, pending: list[float], last: float | None) -> None:
    content = json.dumps(
        {"pending_starts": pending, "last_started_at": last},
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    atomic_write(data / "daemon-health.json", content)


def record_start(data: Path, *, now: float) -> None:
    """Record one owned daemon start without allowing evidence failures to stop startup."""
    try:
        started = _timestamp(now)
        pending, _ = _read(data)
        recent = [value for value in pending if 0 <= started - value <= _WINDOW_SECONDS]
        _write(data, (recent + [started])[-_MAX_STARTS:], started)
    except _EVIDENCE_ERRORS:
        pass


def record_ready(data: Path) -> None:
    """Clear pending starts after the first status heartbeat was published successfully."""
    try:
        _, last = _read(data)
    except _EVIDENCE_ERRORS:
        # A published heartbeat establishes a fresh healthy baseline even after corruption.
        last = None
    try:
        _write(data, [], last)
    except _EVIDENCE_ERRORS:
        pass


def startup_evidence(data: Path, *, now: float) -> dict[str, int | float | None]:
    """Read recent startup evidence without mutating it or exposing local diagnostics."""
    try:
        observed = _timestamp(now)
        pending, last = _read(data)
        return {
            "starts_without_heartbeat": sum(
                0 <= observed - started <= _WINDOW_SECONDS for started in pending
            ),
            "window_seconds": _WINDOW_SECONDS,
            "last_started_at": last,
        }
    except _EVIDENCE_ERRORS:
        return {"unavailable": True}
