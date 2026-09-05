"""Opt-in hook probe: persist allowlisted shapes and hashed IDs, never content."""

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

EVENTS = {
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
    "Notification",
    "Setup",
}
FIELDS = {
    "session_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "source",
    "model",
    "permission_mode",
    "turn_id",
    "prompt_id",
    "tool_use_id",
    "tool_name",
    "tool_input",
    "tool_response",
    "error",
    "is_interrupt",
    "duration_ms",
    "agent_id",
    "agent_type",
    "agent_transcript_path",
    "stop_hook_active",
    "last_assistant_message",
    "trigger",
    "reason",
    "compact_summary",
    "prompt",
    "notification_type",
}
RESPONSE_FIELDS = {
    "stdout",
    "stderr",
    "exit_code",
    "exitCode",
    "output",
    "content",
    "isError",
    "interrupted",
    "duration_ms",
    "wall_time_seconds",
    "session_id",
    "structuredContent",
}


def summarize(payload: dict) -> dict:
    response = payload.get("tool_response")
    event = payload.get("hook_event_name")
    identities = {}
    for key in ("session_id", "turn_id", "tool_use_id", "agent_id", "prompt_id"):
        value = payload.get(key)
        if isinstance(value, str):
            identities[key] = hashlib.sha256(value.encode()).hexdigest()
    summary = {
        "event": event if isinstance(event, str) and event in EVENTS else "unknown",
        "received_ns": time.time_ns(),
        "field_types": {k: type(v).__name__ for k, v in payload.items() if k in FIELDS},
        "identities": identities,
    }
    if isinstance(response, dict):
        summary["tool_response_fields"] = {
            k: type(v).__name__ for k, v in response.items() if k in RESPONSE_FIELDS
        }
        code = response.get("exit_code", response.get("exitCode"))
        if type(code) is int:
            summary["exit_code"] = code
    if payload.get("tool_name") in {"Bash", "PowerShell", "Agent", "apply_patch"}:
        summary["tool_name"] = payload["tool_name"]
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-noop", action="store_true")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        summary = summarize(payload)
        args.output.mkdir(parents=True, exist_ok=True)
        temporary = args.output / f"{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(summary), encoding="utf-8")
        temporary.replace(temporary.with_suffix(".json"))
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    finally:
        if args.json_noop:
            print("{}")


if __name__ == "__main__":
    main()
