"""Codex observation only: bounded, redacted content and no model feedback."""

import json
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from agent_watchdog import daemon, resources
from agent_watchdog.config import Limits, UserPaths, load_config
from agent_watchdog.diagnostics import emit, error_code
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.privacy import redact, sanitize
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

_CONTENT_FIELDS = ("prompt", "tool_input", "tool_response", "last_assistant_message")
_COMMON_FIELDS = {
    "cwd",
    "hook_event_name",
    "session_id",
    "turn_id",
    "agent_id",
    "parent_agent_id",
    "native_event_id",
    "provider_version",
    "surface",
    "source",
    "prompt_id",
    "scratchpad_dir",
    "transcript_path",
    "tool_name",
    "tool_use_id",
    *_CONTENT_FIELDS,
}
_TELEMETRY_FIELDS = {
    "model",
    "model_alias",
    "reasoning_effort",
    "effort",
    "reasoning_mode",
    "client_version",
    "context_window",
    "context_tokens",
    "cached_input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "duration_ms",
    "turn_duration_ms",
    "tool_duration_ms",
    "retry_count",
    "is_interrupt",
    "permission_mode",
    "permission_outcome",
    "error",
    "parent_session_id",
    "parent_turn_id",
}
_PROVIDER_FIELDS = {
    "codex": {
        "trigger",
        "agent_type",
        "agent_transcript_path",
        "stop_hook_active",
        "reason",
    },
    "claude": {
        "trigger",
        "agent_type",
        "agent_transcript_path",
        "stop_hook_active",
        "reason",
        "message",
        "notification_type",
        "background_tasks",
        "session_crons",
        "session_title",
    },
}


def unknown_fields(payload: dict, provider: str) -> tuple[str, ...]:
    """Return unrecognised top-level provider fields, never nested user payload keys."""
    known = _COMMON_FIELDS | _TELEMETRY_FIELDS | _PROVIDER_FIELDS[provider]
    return tuple(sorted(key for key in payload if key not in known))


def warn_unknown_fields(
    paths: UserPaths,
    limits: Limits,
    payload: dict,
    provider: str,
    *,
    component: str,
    event: str,
    project_id: UUID,
    event_id: UUID,
) -> None:
    """Make new provider schema visible without logging provider values."""
    for name in unknown_fields(payload, provider):
        field = name if name.isascii() and name[:1].isalpha() and len(name) <= 64 else "nonstandard"
        emit(
            paths.data,
            limits,
            "WARNING",
            component=component,
            event=event,
            decision="received",
            reason="unknown_field",
            provider=provider,
            field=field,
            project_id=project_id,
            event_id=event_id,
        )


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
        {key: payload[key] for key in _CONTENT_FIELDS if key in payload}
        if limits.capture_content
        else {}
    )
    native = identifier(payload, "hook_event_name")
    metadata = {key: value for key, value in payload.items() if key not in _CONTENT_FIELDS}
    provider_payload = {
        "hook_event_name": native if native in events else "unknown",
        "tool_use_id": identifier(payload, "tool_use_id"),
        "tool_name": identifier(payload, "tool_name"),
        "tool_response_type": type(payload["tool_response"]).__name__
        if "tool_response" in payload
        else None,
        "content": content or "omitted",
        "metadata": metadata,
        "unknown_fields": list(unknown_fields(payload, provider)),
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
        parent_agent_id=identifier(payload, "parent_agent_id"),
        native_event_id=identifier(payload, "native_event_id"),
        provider_version=identifier(payload, "provider_version"),
        kind=events.get(native or "", "unknown"),
        source="hook",
        payload={provider: provider_payload},
        availability={
            "content": "observed" if content else "unavailable",
            "surface": "unknown",
            "provider_version": "unknown",
            "occurred_at": "unavailable",
            "tool_outcome": "unknown",
        }
        | {f"input.{key}": "observed" for key in metadata}
        | {
            f"input.{key}": "observed" if limits.capture_content else "unavailable"
            for key in _CONTENT_FIELDS
            if key in payload
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
        config = load_config(paths.config)
        if daemon.desired(paths).get("paused", False):
            emit(
                paths.data,
                config.defaults,
                "DEBUG",
                component="hook",
                event="observe",
                decision="paused",
            )
            return
        if not config.projects and not config.auto_add_projects:
            emit(
                paths.data,
                config.defaults,
                "DEBUG",
                component="hook",
                event="observe",
                decision="disabled",
            )
            return
        maximum = max(
            (project.overrides.apply(config.defaults).payload_bytes for project in config.projects),
            default=config.defaults.payload_bytes,
        )
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            resources.count_loss(paths.data, "payload")
            emit(
                paths.data,
                config.defaults,
                "WARNING",
                component="hook",
                event="observe",
                decision="discarded",
                reason="oversized",
                bytes=len(raw),
            )
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            resources.count_loss(paths.data, "invalid")
            emit(
                paths.data,
                config.defaults,
                "WARNING",
                component="hook",
                event="observe",
                decision="discarded",
                reason="invalid",
            )
            return
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            resources.count_loss(paths.data, "invalid")
            emit(
                paths.data,
                config.defaults,
                "WARNING",
                component="hook",
                event="observe",
                decision="discarded",
                reason="invalid",
                field="cwd",
            )
            return
        input_payload = redact({key: value for key, value in payload.items() if key != "cwd"})
        if not isinstance(input_payload, dict):
            raise ValueError("Invalid hook input")
        resolution = Registry(config).resolve(Path(cwd), timeout=0.25)
        if resolution is None and config.auto_add_projects:
            config, resolution = daemon.resolve_or_auto_register(paths, Path(cwd), timeout=0.25)
        if resolution is None:
            emit(
                paths.data,
                config.defaults,
                "DEBUG",
                component="hook",
                event="observe",
                decision="unregistered",
            )
            return
        project = next(item for item in config.projects if item.id == resolution.project_id)
        limits = project.overrides.apply(config.defaults)
        if len(raw) > limits.payload_bytes:
            resources.count_loss(paths.project_data(project.id), "payload")
            emit(
                paths.data,
                config.defaults,
                "WARNING",
                component="hook",
                event="observe",
                decision="discarded",
                reason="oversized",
                project_id=project.id,
                bytes=len(raw),
            )
            return
        event = build_envelope(input_payload, resolution, limits, provider)
        warn_unknown_fields(
            paths,
            config.defaults,
            input_payload,
            provider,
            component="hook",
            event="observe",
            project_id=project.id,
            event_id=event.event_id,
        )
        # Admission records project-level rejection counts itself.
        try:
            admitted = daemon.enqueue(paths, sanitize(event))
            emit(
                paths.data,
                config.defaults,
                "DEBUG",
                component="hook",
                event="observe",
                decision="admitted" if admitted else "paused",
                project_id=project.id,
                event_id=event.event_id,
            )
        except Exception as error:
            emit(
                paths.data,
                config.defaults,
                "WARNING",
                component="hook",
                event="observe",
                decision="failed",
                error_type=error_code(error),
                project_id=project.id,
                event_id=event.event_id,
                detail=str(error),
            )
            return
    except Exception as error:
        # Hooks must fail open even when a dependency or an unexpected payload fails.
        resources.count_loss(paths.data, "invalid")
        emit(
            paths.data,
            Limits(),
            "WARNING",
            component="hook",
            event="observe",
            decision="failed",
            error_type=error_code(error),
            detail=str(error),
        )
        return
