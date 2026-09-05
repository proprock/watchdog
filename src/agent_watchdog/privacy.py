"""Deterministic removal of known credential forms, never a general secret detector."""

import json
import re

from pydantic import JsonValue

from agent_watchdog.events import Envelope

REDACTED = "[REDACTED]"
_KEY = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)"
)
_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?"
        r"(?:-----END (?:[A-Z ]*PRIVATE KEY)-----|\Z)"
    ),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>]+"),
    re.compile(
        r"(?i)(?:password|passwd|secret(?:[_-]access[_-]key)?|token|"
        r"api[_-]?key|authorization|cookie)"
        r"[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
)


def text(value: str) -> str:
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            safe_key = text(key)
            # Never silently merge two provider fields after redaction.
            if safe_key in result:
                safe_key = f"{safe_key}:{len(result)}"
            result[safe_key] = REDACTED if _KEY.search(key) else redact(item)
        return result
    return value


def sanitize(event: Envelope) -> Envelope:
    # Redact free-form identifiers as well as the provider payload.
    value = event.model_dump(mode="json")
    for key in (
        "provider",
        "session_id",
        "agent_id",
        "parent_agent_id",
        "turn_id",
        "native_event_id",
        "provider_version",
    ):
        if isinstance(value[key], str):
            value[key] = text(value[key])
    value["payload"] = redact(value["payload"])
    value["availability"] = {text(key): item for key, item in value["availability"].items()}
    return Envelope.model_validate_json(json.dumps(value))


def artifact(data: bytes) -> bytes:
    # Binary artifacts cannot be inspected reliably; this collector stores text only.
    decoded = data.decode("utf-8")
    try:
        value = json.loads(decoded)
    except ValueError:
        return text(decoded).encode("utf-8")
    return json.dumps(redact(value), ensure_ascii=False).encode("utf-8")
