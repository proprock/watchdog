"""Codex observation only: bounded, redacted content and no model feedback."""

import json
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from agent_watchdog import daemon, resources
from agent_watchdog.config import Limits, UserPaths, load_config
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.privacy import sanitize
from agent_watchdog.registry import Registry, Resolution

EVENTS: dict[str, dict[str, EventKind]] = {
    "codex": {
        "SessionStart": "session.start",
        "SessionEnd": "session.end",
        "UserPromptSubmit": "turn.start",
        "PreToolUse": "tool.start",
        "PostToolUse": "tool.finish",
        "PreCompact": "compaction.start",
        "PostCompact": "compaction.end",
        "SubagentStart": "agent.start",
        "SubagentStop": "agent.end",
        "Stop": "turn.end",
        "Interrupt": "interrupt",
    },
    "claude": {
        "SessionStart": "session.start",
        "SessionEnd": "session.end",
        "UserPromptSubmit": "turn.start",
        "Stop": "turn.end",
        "PreToolUse": "tool.start",
        "PostToolUse": "tool.finish",
        "PostToolUseFailure": "tool.finish",
        "PreCompact": "compaction.start",
        "PostCompact": "compaction.end",
        "SubagentStart": "agent.start",
        "SubagentStop": "agent.end",
        "Notification": "waiting",
    },
}


def identifier(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    return value if isinstance(value, str) and value.strip() and len(value) <= 256 else None


def transcript_path(payload: dict) -> str | None:
    value = payload.get("transcript_path")
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return None
    path = Path(value)
    return str(path) if path.is_absolute() else None


def build_envelope(
    payload: dict,
    resolution: Resolution,
    limits: Limits,
    provider: str,
    *,
    event_id: UUID | None = None,
    received_at: datetime | None = None,
) -> Envelope:
    """Shared hook-envelope construction for the Python path and the spool drain.

    ``event_id`` / ``received_at`` are supplied only when replaying a spool record
    stamped by the native adapter; otherwise the model defaults apply.
    """
    events = EVENTS[provider]
    content = (
        {
            key: payload[key]
            for key in ("prompt", "tool_input", "tool_response", "last_assistant_message")
            if key in payload
        }
        if limits.capture_content
        else {}
    )
    native = identifier(payload, "hook_event_name")
    provider_payload = {
        "hook_event_name": native if native in events else "unknown",
        "tool_use_id": identifier(payload, "tool_use_id"),
        "tool_name": identifier(payload, "tool_name"),
        "tool_response_type": type(payload["tool_response"]).__name__
        if "tool_response" in payload
        else None,
        "content": content or "omitted",
    }
    path = transcript_path(payload) if provider == "codex" else None
    if path is not None:
        provider_payload["transcript_path"] = path
    envelope = Envelope(
        provider=provider,
        project_id=resolution.project_id,
        checkout_id=resolution.checkout_id,
        session_id=identifier(payload, "session_id"),
        turn_id=identifier(payload, "turn_id"),
        agent_id=identifier(payload, "agent_id"),
        kind=events.get(native or "", "unknown"),
        source="hook",
        payload={provider: provider_payload},
        availability={
            "content": "observed" if content else "unavailable",
            "surface": "unknown",
            "provider_version": "unknown",
            "occurred_at": "unavailable",
            "tool_outcome": "unknown",
        },
    )
    replay: dict[str, object] = {}
    if event_id is not None:
        replay["event_id"] = event_id
    if received_at is not None:
        replay["received_at"] = received_at
    return envelope.model_copy(update=replay) if replay else envelope


def observe(paths: UserPaths, stream: BinaryIO, provider: str = "codex") -> None:
    if provider not in EVENTS:
        raise KeyError(provider)
    try:
        if daemon.desired(paths).get("paused", False):
            return
        config = load_config(paths.config)
        if not config.projects and not config.auto_add_projects:
            return
        maximum = max(
            (project.overrides.apply(config.defaults).payload_bytes for project in config.projects),
            default=config.defaults.payload_bytes,
        )
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            resources.count_loss(paths.data, "payload")
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            resources.count_loss(paths.data, "invalid")
            return
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            resources.count_loss(paths.data, "invalid")
            return
        resolution = Registry(config).resolve(Path(cwd), timeout=0.25)
        if resolution is None and config.auto_add_projects:
            config, resolution = daemon.resolve_or_auto_register(paths, Path(cwd), timeout=0.25)
        if resolution is None:
            return
        project = next(item for item in config.projects if item.id == resolution.project_id)
        limits = project.overrides.apply(config.defaults)
        if len(raw) > limits.payload_bytes:
            resources.count_loss(paths.project_data(project.id), "payload")
            return
        event = build_envelope(payload, resolution, limits, provider)
        # Admission records project-level rejection counts itself.
        try:
            daemon.enqueue(paths, sanitize(event))
        except Exception:
            return
    except Exception:
        # Hooks must fail open even when a dependency or an unexpected payload fails.
        resources.count_loss(paths.data, "invalid")
        return
