"""Codex observation only: bounded, redacted content and no model feedback."""

import json
from pathlib import Path
from typing import BinaryIO

from agent_watchdog import daemon, resources
from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.privacy import sanitize
from agent_watchdog.registry import Registry

EVENTS: dict[str, EventKind] = {
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
}


def identifier(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    return value if isinstance(value, str) and value.strip() and len(value) <= 256 else None


def observe(paths: UserPaths, stream: BinaryIO) -> None:
    try:
        if daemon.desired(paths).get("paused", False):
            return
        config = load_config(paths.config)
        if not config.projects:
            return
        maximum = max(
            project.overrides.apply(config.defaults).payload_bytes for project in config.projects
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
        if resolution is None:
            return
        project = next(item for item in config.projects if item.id == resolution.project_id)
        limits = project.overrides.apply(config.defaults)
        if len(raw) > limits.payload_bytes:
            resources.count_loss(paths.project_data(project.id), "payload")
            return
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
        event = Envelope(
            provider="codex",
            project_id=resolution.project_id,
            checkout_id=resolution.checkout_id,
            session_id=identifier(payload, "session_id"),
            turn_id=identifier(payload, "turn_id"),
            agent_id=identifier(payload, "agent_id"),
            kind=EVENTS.get(native or "", "unknown"),
            source="hook",
            payload={
                "codex": {
                    "hook_event_name": native if native in EVENTS else "unknown",
                    "tool_use_id": identifier(payload, "tool_use_id"),
                    "tool_name": identifier(payload, "tool_name"),
                    "tool_response_type": type(payload["tool_response"]).__name__
                    if "tool_response" in payload
                    else None,
                    "content": content or "omitted",
                }
            },
            availability={
                "content": "observed" if content else "unavailable",
                "surface": "unknown",
                "provider_version": "unknown",
                "occurred_at": "unavailable",
                "tool_outcome": "unknown",
            },
        )
        # Admission records project-level rejection counts itself.
        try:
            daemon.enqueue(paths, sanitize(event))
        except Exception:
            return
    except Exception:
        # Hooks must fail open even when a dependency or an unexpected payload fails.
        resources.count_loss(paths.data, "invalid")
        return
