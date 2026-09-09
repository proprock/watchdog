"""Derived schema-v6 telemetry projections for the ``event_facts`` table (WD-111).

``events.envelope`` stays the authoritative document. ``event_facts`` holds
nullable, typed copies of the fields WD-112 aggregates so normal analysis never
runs ``json_extract``. Every value here is read from the provider metadata
namespace (``payload.<provider>.metadata`` or the transcript reader's own
projection), never from captured tool input or output.
"""

from datetime import datetime
from typing import Any

# INSERT / CREATE TABLE column order.
KEY_COLUMNS = ("event_id", "provider", "kind", "received_at_us", "session_id", "turn_id")
CONTEXT_COLUMNS = (
    "turn_id_source",
    "conversation_id_source",
    "source",
    "surface",
    "checkout_id",
    "agent_id",
    "parent_agent_id",
    "native_event_id",
    "occurred_at_us",
)
CONFIG_COLUMNS = (
    "model",
    "model_attribution",
    "reasoning_effort",
    "reasoning_effort_attribution",
)
USAGE_COLUMNS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
PROCESS_COLUMNS = (
    "hook_event_name",
    "tool_name",
    "tool_use_id",
    "tool_duration_ms",
    "permission_mode",
    "agent_type",
    "notification_type",
)
FACT_COLUMNS = KEY_COLUMNS + CONTEXT_COLUMNS + CONFIG_COLUMNS + USAGE_COLUMNS + PROCESS_COLUMNS

_INT_COLUMN = {
    "received_at_us",
    "occurred_at_us",
    "tool_duration_ms",
    *USAGE_COLUMNS,
}
_TABLE_COLUMNS = ", ".join(
    f"{name} {'INTEGER' if name in _INT_COLUMN else 'TEXT'}"
    + (" PRIMARY KEY REFERENCES events(event_id)" if name == "event_id" else "")
    for name in FACT_COLUMNS
)
CREATE_TABLE = f"CREATE TABLE event_facts ({_TABLE_COLUMNS})"
INSERT = f"INSERT INTO event_facts VALUES ({', '.join('?' for _ in FACT_COLUMNS)})"

CREATE_INDEXES = (
    "CREATE INDEX event_facts_context "
    "ON event_facts(provider, session_id, turn_id, received_at_us)",
    "CREATE INDEX event_facts_usage_time "
    "ON event_facts(received_at_us, provider, model, reasoning_effort) WHERE kind = 'usage'",
    "CREATE INDEX event_facts_usage_dim "
    "ON event_facts(provider, model, reasoning_effort, received_at_us) WHERE kind = 'usage'",
    "CREATE INDEX event_facts_tool_pair "
    "ON event_facts(provider, session_id, turn_id, tool_use_id) "
    "WHERE kind IN ('tool.start', 'tool.finish')",
    "CREATE INDEX event_facts_tool_cost "
    "ON event_facts(provider, tool_name, received_at_us) WHERE kind = 'tool.finish'",
)


def epoch_us(value: object) -> int | None:
    """Convert an aware ISO-8601 timestamp to integer microseconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return round(parsed.timestamp() * 1_000_000)


def _namespace(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = envelope.get("payload")
    provider = envelope.get("provider")
    namespace = payload.get(provider) if isinstance(payload, dict) else None
    return namespace if isinstance(namespace, dict) else {}


def _metadata(envelope: dict[str, Any]) -> dict[str, Any]:
    metadata = _namespace(envelope).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def turn_of(envelope: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(turn_id, turn_id_source)`` for one envelope.

    Claude's turn key is ``metadata.prompt_id`` (also promoted onto the envelope
    for new hooks); Codex carries ``turn_id`` directly on the envelope and in
    ``metadata``. Claude usage rows have neither.
    """
    metadata = _metadata(envelope)
    if envelope.get("provider") == "claude":
        prompt_id = _text(metadata.get("prompt_id")) or _text(envelope.get("turn_id"))
        return (prompt_id, "prompt_id") if prompt_id is not None else (None, "unavailable")
    turn_id = _text(envelope.get("turn_id")) or _text(metadata.get("turn_id"))
    return (turn_id, "turn_id") if turn_id is not None else (None, "unavailable")


def _conversation_source(envelope: dict[str, Any]) -> str:
    if envelope.get("provider") == "codex" and envelope.get("kind") == "usage":
        # events.session_id already holds payload.codex.thread_id (observed equal).
        return "thread_id"
    return "session_id" if _text(envelope.get("session_id")) is not None else "unavailable"


def observed_model(envelope: dict[str, Any]) -> str | None:
    """Model read straight from provider metadata, or a usage reader's projection."""
    direct = _text(_metadata(envelope).get("model"))
    if direct is not None:
        return direct
    if envelope.get("kind") == "usage":
        return _text(_namespace(envelope).get("model"))
    return None


def observed_effort(envelope: dict[str, Any]) -> str | None:
    """Reasoning effort: Claude's nested ``effort.level`` or Codex's flat key."""
    metadata = _metadata(envelope)
    nested = metadata.get("effort")
    if isinstance(nested, dict):
        level = _text(nested.get("level"))
        if level is not None:
            return level
    elif _text(nested) is not None:  # tolerate a flat ``effort`` string
        return _text(nested)
    return _text(metadata.get("reasoning_effort"))


def _usage(envelope: dict[str, Any]) -> dict[str, int | None]:
    empty: dict[str, int | None] = {name: None for name in USAGE_COLUMNS}
    if envelope.get("kind") != "usage":
        return empty
    usage = _namespace(envelope).get("usage")
    if not isinstance(usage, dict):
        return empty
    if envelope.get("provider") == "codex":
        delta = usage.get("delta")
        if not isinstance(delta, dict):
            return empty
        return {name: _count(delta.get(name)) for name in USAGE_COLUMNS}
    response = usage.get("response")
    if not isinstance(response, dict):
        return empty
    return {
        "input_tokens": _count(response.get("input_tokens")),
        "cached_input_tokens": _count(response.get("cache_read_input_tokens")),
        "cache_write_input_tokens": _count(response.get("cache_creation_input_tokens")),
        "output_tokens": _count(response.get("output_tokens")),
        "reasoning_output_tokens": _count(response.get("thinking_tokens")),
        "total_tokens": None,  # Anthropic reports no total; never synthesise one.
    }


def _process(envelope: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(envelope)
    duration = metadata.get("duration_ms")
    return {
        "hook_event_name": _text(metadata.get("hook_event_name")),
        "tool_name": _text(metadata.get("tool_name")),
        "tool_use_id": _text(metadata.get("tool_use_id")),
        "tool_duration_ms": (_count(duration) if envelope.get("kind") == "tool.finish" else None),
        "permission_mode": _text(metadata.get("permission_mode")),
        "agent_type": _text(metadata.get("agent_type")),
        "notification_type": _text(metadata.get("notification_type")),
    }


def _attributed(
    row_value: str | None, inherited: str | None, *, is_usage: bool
) -> tuple[str | None, str]:
    if row_value is not None:
        return row_value, "observed"
    if is_usage and inherited is not None:
        return inherited, "inherited"
    return None, "unavailable"


def project_event(
    event_id: str,
    kind: str,
    received_at: str,
    envelope: dict[str, Any],
    *,
    inherited: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    """Build one ``event_facts`` row from a persisted envelope document.

    ``inherited`` is the latest earlier observed ``(model, reasoning_effort)`` in
    the same conversation, applied only to ``usage`` rows that lack their own
    observation.
    """
    is_usage = kind == "usage"
    turn_id, turn_id_source = turn_of(envelope)
    model, model_attribution = _attributed(
        observed_model(envelope), inherited[0], is_usage=is_usage
    )
    effort, effort_attribution = _attributed(
        observed_effort(envelope), inherited[1], is_usage=is_usage
    )
    row: dict[str, Any] = {
        "event_id": event_id,
        "provider": _text(envelope.get("provider")),
        "kind": kind,
        "received_at_us": epoch_us(received_at),
        "session_id": _text(envelope.get("session_id")),
        "turn_id": turn_id,
        "turn_id_source": turn_id_source,
        "conversation_id_source": _conversation_source(envelope),
        "source": _text(envelope.get("source")),
        "surface": _text(envelope.get("surface")),
        "checkout_id": _text(envelope.get("checkout_id")),
        "agent_id": _text(envelope.get("agent_id")),
        "parent_agent_id": _text(envelope.get("parent_agent_id")),
        "native_event_id": _text(envelope.get("native_event_id")),
        "occurred_at_us": epoch_us(envelope.get("occurred_at")),
        "model": model,
        "model_attribution": model_attribution,
        "reasoning_effort": effort,
        "reasoning_effort_attribution": effort_attribution,
        **_usage(envelope),
        **_process(envelope),
    }
    return row


def observation(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the ``(model, effort)`` a row contributes to later inheritance."""
    return (
        row["model"] if row["model_attribution"] == "observed" else None,
        row["reasoning_effort"] if row["reasoning_effort_attribution"] == "observed" else None,
    )


class ConfigTracker:
    """Insertion-order resolver for same-conversation model/effort inheritance."""

    def __init__(self) -> None:
        self._by_turn: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        self._by_conversation: dict[str, tuple[str | None, str | None]] = {}

    @staticmethod
    def _merge(
        old: tuple[str | None, str | None] | None, new: tuple[str | None, str | None]
    ) -> tuple[str | None, str | None]:
        if old is None:
            return new
        return (new[0] if new[0] is not None else old[0], new[1] if new[1] is not None else old[1])

    def observe(
        self, session_id: str | None, turn_id: str | None, model: str | None, effort: str | None
    ) -> None:
        if session_id is None or (model is None and effort is None):
            return
        value = (model, effort)
        self._by_conversation[session_id] = self._merge(
            self._by_conversation.get(session_id), value
        )
        if turn_id is not None:
            key = (session_id, turn_id)
            self._by_turn[key] = self._merge(self._by_turn.get(key), value)

    def resolve(self, session_id: str | None, turn_id: str | None) -> tuple[str | None, str | None]:
        if session_id is None:
            return (None, None)
        if turn_id is not None and (session_id, turn_id) in self._by_turn:
            return self._by_turn[(session_id, turn_id)]
        return self._by_conversation.get(session_id, (None, None))
