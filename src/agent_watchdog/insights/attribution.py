"""Time-scoped observed configurations for delegation reports."""

from typing import Any

from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline


def at_us(event: Envelope) -> int:
    """Use provider time even when transcript ingestion happened much later."""
    return timeline.us(event.occurred_at or event.received_at) or 0


def context_configuration(event: Envelope) -> dict[str, Any] | None:
    metadata = timeline.metadata(event)
    if (
        event.provider != "codex"
        or event.source != "transcript"
        or event.occurred_at is None
        or metadata.get("record_type") != "turn_context"
        or metadata.get("attribution_projection") != "wd-165.v1"
    ):
        return None
    return {
        "at_us": at_us(event),
        "model": _text(metadata.get("model")),
        "reasoning_effort": _text(metadata.get("reasoning_effort")),
        "evidence_id": str(event.event_id),
        "source": "turn_context",
        "model_attribution": "observed" if _text(metadata.get("model")) else "unknown",
        "reasoning_effort_attribution": (
            "observed" if _text(metadata.get("reasoning_effort")) else "unknown"
        ),
    }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def request_configuration(request: timeline.Request) -> dict[str, Any]:
    return {
        "at_us": request.at_us,
        "model": request.model,
        "reasoning_effort": getattr(request, "reasoning_effort", None),
        "evidence_id": request.event_id,
        "source": "usage",
        "response_time_attribution": "native" if request.native_time_available else "unavailable",
        "model_attribution": getattr(request, "model_attribution", "unknown"),
        "reasoning_effort_attribution": getattr(request, "reasoning_effort_attribution", "unknown"),
    }


def configuration_at(
    sequence: list[timeline.Request], contexts: list[Envelope], at: int
) -> dict[str, Any]:
    """Latest snapshot, with absent values kept unknown across context switches."""
    snapshots = [
        request_configuration(row)
        for row in sequence
        if row.native_time_available and row.at_us <= at
    ]
    snapshots.extend(
        value
        for event in contexts
        if (value := context_configuration(event)) is not None and value["at_us"] <= at
    )
    if not snapshots:
        return {
            "at_us": None,
            "model": None,
            "reasoning_effort": None,
            "evidence_id": None,
            "source": "unknown",
            "model_attribution": "unknown",
            "reasoning_effort_attribution": "unknown",
        }
    latest = max(value["at_us"] for value in snapshots)
    candidates = [value for value in snapshots if value["at_us"] == latest]
    # Conflicting contexts at the same provider time have no defensible ordering.
    result = dict(candidates[0])
    result["evidence_ids"] = sorted({value["evidence_id"] for value in candidates})
    for field in ("model", "reasoning_effort"):
        if len({value[field] for value in candidates}) > 1:
            result[field] = None
            result[f"{field}_attribution"] = "unknown"
    return result
