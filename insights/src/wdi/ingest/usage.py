"""Reconcile provider notifications without discarding original observations."""

from __future__ import annotations

from typing import Any

from wdi.ingest.models import ContextEventRec, MessageRec, ParsedChunk

COUNTERS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def reconcile_usage(chunk: ParsedChunk) -> ParsedChunk:
    seen: dict[str, ContextEventRec] = {}
    indexes: dict[int, int] = {}
    next_call = min(
        (e.model_call_index or 0 for e in chunk.context_events if e.event_type == "model_call"),
        default=0,
    )
    for event in chunk.context_events:
        if event.event_type != "model_call":
            continue
        extra = event.extra or {}
        event.extra = extra
        extra["raw_usage"] = {key: getattr(event, key) for key in COUNTERS}
        if not any(getattr(event, name) for name in COUNTERS):
            indexes[event.model_call_index or 0] = next_call
            event.event_type = "usage_observation"
            extra["usage_method"] = "empty_or_reset_notification"
            continue
        key = extra.get("usage_key")
        previous = seen.get(key) if isinstance(key, str) else None
        old_index = event.model_call_index or 0
        if previous is None:
            event.model_call_index = next_call
            indexes[old_index] = next_call
            next_call += 1
            if isinstance(key, str):
                seen[key] = event
        else:
            indexes[old_index] = previous.model_call_index or 0
            event.model_call_index = previous.model_call_index
            event.event_type = "usage_observation"
            extra["duplicate_of"] = previous.idx
            for name in COUNTERS:
                values = [
                    v for v in (getattr(previous, name), getattr(event, name)) if v is not None
                ]
                setattr(previous, name, max(values) if values else None)
            if previous.extra is not None:
                previous.extra["merged_observations"] = (
                    int(previous.extra.get("merged_observations", 1)) + 1
                )
                previous.extra["usage_method"] = "provider_identity_component_max"
    for event in chunk.context_events:
        if event.event_type not in ("model_call", "usage_observation"):
            event.model_call_index = indexes.get(event.model_call_index or 0, next_call)
    messages: dict[str, MessageRec] = {}
    for message in chunk.messages:
        extra = message.extra or {}
        key = extra.get("usage_key")
        if not isinstance(key, str):
            continue
        message.extra = extra
        extra["raw_usage"] = {name: getattr(message, name) for name in COUNTERS}
        previous_message = messages.get(key)
        if previous_message is None:
            messages[key] = message
        else:
            extra["duplicate_usage"] = True
            for name in COUNTERS:
                values = [
                    v
                    for v in (getattr(previous_message, name), getattr(message, name))
                    if v is not None
                ]
                setattr(previous_message, name, max(values) if values else None)
                setattr(message, name, None)
    outcomes: dict[str, Any] = {}
    for result in chunk.tool_results:
        if result.is_error is not None:
            outcomes.setdefault(result.call_id, result.is_error)
    for event in chunk.context_events:
        extra = event.extra or {}
        if extra.get("call_id") in outcomes:
            extra["is_error"] = outcomes[extra["call_id"]]
            event.extra = extra
    return chunk
