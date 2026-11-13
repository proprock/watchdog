"""Parser and importer for OpenAI Codex rollout files.

Layout (see docs/codex-session-metadata.md):
    <root>/YYYY/MM/DD/rollout-<ISO8601>-<sessionId>.jsonl   one file = one session

Each line is an envelope {type, timestamp, payload}. Tokens arrive as separate
token_count events (there is no usage on assistant messages), so they are stored
as role='event' rows and summed by the shared aggregate recompute.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wdi.db.repository import Repository
from wdi.ingest.context import ContextTracker, json_text, tool_output_category
from wdi.ingest.models import (
    INPUT_PREVIEW_MAX,
    MAIN_STREAM,
    PREVIEW_MAX,
    USER_TEXT_MAX,
    ImportProgress,
    ImportStats,
    MessageRec,
    ParsedChunk,
    ToolCallRec,
    ToolResultRec,
    truncate,
)
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.runner import import_stream_file
from wdi.ingest.usage import reconcile_usage

_ROLLOUT_FILE = re.compile(
    r"rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

# CLI >= ~0.129 stopped emitting exec_command_end; exit code and wall time then
# exist only as this text header inside function_call_output.output.
_EXEC_OUTPUT_HEADER = re.compile(r"^Exit code: (-?\d+)\r?\nWall time: ([\d.]+) seconds?\r?\n")

_TOOL_CATEGORY = {
    "shell_command": "exec",
    "shell": "exec",
    "apply_patch": "edit",
    "view_image": "read",
    "web_search": "web",
    "update_plan": "other",
    "request_user_input": "other",
}


def _duration_to_ms(duration: Any) -> int | None:
    if isinstance(duration, dict):
        secs = duration.get("secs")
        nanos = duration.get("nanos")
        if isinstance(secs, int) and isinstance(nanos, int):
            return secs * 1000 + nanos // 1_000_000
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _hash_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _byte_len(value: Any) -> int | None:
    return len(value.encode("utf-8")) if isinstance(value, str) else None


def _shell_target(arguments: Any) -> str | None:
    if not isinstance(arguments, str):
        return None
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        command = parsed.get("command")
        if isinstance(command, str):
            return truncate(command, 200)
    return None


def _command_kind(parsed_cmd: Any, fallback_command: str | None) -> str | None:
    if isinstance(parsed_cmd, list) and parsed_cmd:
        first = parsed_cmd[0]
        if isinstance(first, dict):
            for key in ("program", "cmd", "name"):
                value = first.get(key)
                if isinstance(value, str) and value:
                    return value
    if fallback_command:
        return fallback_command.split(maxsplit=1)[0]
    return None


def _parse_custom_tool_output(call_id: str, payload: dict[str, Any]) -> ToolResultRec:
    """custom_tool_call_output.output is JSON text: {"output": ..., "metadata": {...}}."""
    raw = payload.get("output")
    preview = raw if isinstance(raw, str) else None
    exit_code: int | None = None
    duration_ms: int | None = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed.get("output")
            if isinstance(inner, str) and inner:
                preview = inner
            metadata = parsed.get("metadata")
            if isinstance(metadata, dict):
                exit_code = _int_or_none(metadata.get("exit_code"))
                seconds = metadata.get("duration_seconds")
                if isinstance(seconds, int | float):
                    duration_ms = int(seconds * 1000)
    return ToolResultRec(
        call_id=call_id,
        is_error=None if exit_code is None else exit_code != 0,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output_bytes=_byte_len(preview),
        output_preview=truncate(preview, PREVIEW_MAX),
    )


def _subagent_kind(sub: Any) -> str | None:
    """Extract the subagent kind from session_meta.source, e.g. {"other": "guardian"}."""
    if isinstance(sub, str) and sub:
        return sub
    if isinstance(sub, dict):
        for value in sub.values():
            if isinstance(value, str) and value:
                return value
        for key in sub:
            if isinstance(key, str) and key:
                return key
    return None


def _patch_line_counts(changes: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(changes, list):
        return None, None, None
    added = 0
    removed = 0
    files = 0
    for change in changes:
        if not isinstance(change, dict):
            continue
        files += 1
        diff = change.get("unified_diff")
        if not isinstance(diff, str):
            continue
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return files, added, removed


class _CodexParser:
    def __init__(
        self,
        msg_idx0: int,
        tool_idx0: int,
        context_idx0: int = 0,
        model_call_idx0: int = 0,
        generation0: int = 0,
        previous_context_event: str | None = None,
    ) -> None:
        self.chunk = ParsedChunk()
        self.msg_idx = msg_idx0
        self.tool_idx = tool_idx0
        self.current_model: str | None = None
        self.context = ContextTracker(
            self.chunk.context_events,
            event_idx0=context_idx0,
            model_call_idx0=model_call_idx0,
            generation0=generation0,
            previous_event_type=previous_context_event,
        )
        self.call_commands: dict[str, str] = {}
        self.call_tools: dict[str, str] = {}

    def _add_message(self, rec: MessageRec) -> None:
        self.chunk.messages.append(rec)
        self.msg_idx += 1

    def _add_tool_call(self, rec: ToolCallRec) -> None:
        self.chunk.tool_calls.append(rec)
        self.tool_idx += 1

    def feed(self, ts: str | None, record_type: str, payload: dict[str, Any]) -> None:
        fields = self.chunk.session_fields
        if record_type == "session_meta":
            self.context.add_text(
                event_type="instruction",
                category="instructions",
                text=json_text(payload.get("base_instructions")),
                ts=ts,
                role="system",
                slot="base",
            )
            for src, dst in (
                ("cwd", "cwd"),
                ("cli_version", "cli_version"),
                ("originator", "originator"),
            ):
                value = payload.get(src)
                if isinstance(value, str) and value:
                    fields[dst] = value
            git = payload.get("git")
            if isinstance(git, dict) and isinstance(git.get("branch"), str):
                fields["git_branch"] = git["branch"]
            source = payload.get("source")
            if isinstance(source, str) and source:
                fields["entrypoint"] = source
            elif isinstance(source, dict) and "subagent" in source:
                kind = _subagent_kind(source.get("subagent"))
                fields["entrypoint"] = f"subagent:{kind}" if kind else "subagent"
            parent = payload.get("parent_thread_id")
            if isinstance(parent, str) and parent:
                fields["parent_session_id"] = parent
        elif record_type == "turn_context":
            self.context.set_turn(payload.get("turn_id"))
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.current_model = model
                fields["model"] = model
            for src, dst in (
                ("effort", "effort"),
                ("approval_policy", "approval_policy"),
                ("collaboration_mode", "collaboration_mode"),
            ):
                value = payload.get(src)
                if isinstance(value, str) and value:
                    fields[dst] = value
            sandbox = payload.get("sandbox_policy")
            if isinstance(sandbox, dict) and isinstance(sandbox.get("type"), str):
                fields["sandbox_policy_type"] = sandbox["type"]
            dev_hash = _hash_text(payload.get("developer_instructions"))
            user_hash = _hash_text(payload.get("user_instructions"))
            if dev_hash:
                fields["developer_instructions_hash"] = dev_hash
            if user_hash:
                fields["user_instructions_hash"] = user_hash
            for key, role in (
                ("developer_instructions", "developer"),
                ("user_instructions", "user"),
            ):
                value = payload.get(key)
                self.context.add_text(
                    event_type="instruction",
                    category="instructions",
                    text=value if isinstance(value, str) else None,
                    ts=ts,
                    role=role,
                    slot=key,
                )
        elif record_type == "response_item":
            self._feed_response_item(ts, payload)
        elif record_type == "event_msg":
            self._feed_event(ts, payload)
        elif record_type == "compacted":
            self._feed_compacted(ts, payload)

    def _add_response_context(
        self, ts: str | None, payload: dict[str, Any], *, replacement: bool = False
    ) -> None:
        ptype = payload.get("type")
        direction = "input" if replacement else "output"
        if ptype == "message":
            role = payload.get("role") if isinstance(payload.get("role"), str) else None
            if not replacement and role != "assistant":
                return
            for block in payload.get("content") or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                category = "user_prompt" if role == "user" else "assistant_text"
                if role in ("developer", "system"):
                    category = "instructions"
                self.context.add_text(
                    event_type="message",
                    category=category,
                    text=text,
                    ts=ts,
                    role=role,
                    direction=direction,
                    replacement=replacement,
                    slot=(
                        f"replacement:{role}"
                        if replacement and role in ("system", "developer")
                        else None
                    ),
                )
        elif ptype in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or "unknown")
            raw_input = (
                payload.get("arguments") if ptype == "function_call" else payload.get("input")
            )
            text = json_text(raw_input)
            command = (_shell_target(raw_input) or text) if ptype == "function_call" else text
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                self.call_tools[call_id] = name
            if isinstance(call_id, str) and command:
                self.call_commands[call_id] = command
            self.context.add_text(
                event_type="tool_input",
                category="tool_input",
                text=text,
                ts=ts,
                role="assistant",
                tool_name=name,
                call_id=call_id if isinstance(call_id, str) else None,
                command=command,
                target=command,
                direction=direction,
                replacement=replacement,
            )
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            output = payload.get("output")
            if ptype == "custom_tool_call_output" and isinstance(output, str):
                try:
                    wrapper = json.loads(output)
                except json.JSONDecodeError:
                    wrapper = None
                if isinstance(wrapper, dict) and "output" in wrapper:
                    output = wrapper["output"]
            text = json_text(output)
            command = self.call_commands.get(call_id) if isinstance(call_id, str) else None
            tool_name = (
                self.call_tools.get(call_id, "unknown") if isinstance(call_id, str) else "unknown"
            )
            self.context.add_text(
                event_type="tool_output",
                category=tool_output_category(tool_name, command),
                text=text,
                ts=ts,
                role="tool",
                tool_name=tool_name,
                call_id=call_id if isinstance(call_id, str) else None,
                command=command,
                target=command,
                direction="input",
                replacement=replacement,
            )
        elif ptype == "reasoning":
            summaries = payload.get("summary") or []
            visible = False
            for block in summaries:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    visible = True
                    self.context.add_text(
                        event_type="reasoning_summary",
                        category="reasoning_summary",
                        text=block["text"],
                        ts=ts,
                        role="assistant",
                        direction=direction,
                        replacement=replacement,
                    )
            if payload.get("encrypted_content") and not visible:
                self.context.add_opaque("opaque_reasoning", ts, direction=direction)
        elif ptype == "compaction":
            self.context.add_opaque("opaque_compaction", ts, direction="input")
        elif ptype == "web_search_call":
            action = payload.get("action")
            query = action.get("query") if isinstance(action, dict) else None
            self.context.add_text(
                event_type="tool_input",
                category="tool_input",
                text=query if isinstance(query, str) else None,
                ts=ts,
                role="assistant",
                tool_name="web_search",
                target=query if isinstance(query, str) else None,
                direction=direction,
                replacement=replacement,
            )

    def _feed_compacted(self, ts: str | None, payload: dict[str, Any]) -> None:
        self.context.add_compaction(ts, trigger="unknown", payload=True)
        history = payload.get("replacement_history")
        if not isinstance(history, list):
            self.context.add_opaque("opaque_compaction", ts, direction="input")
            return
        for item in history:
            if isinstance(item, dict):
                self._add_response_context(ts, item, replacement=True)

    def _feed_response_item(self, ts: str | None, payload: dict[str, Any]) -> None:
        self._add_response_context(ts, payload)
        ptype = payload.get("type")
        if ptype == "message":
            # user/developer roles duplicate event_msg/user_message and instructions
            if payload.get("role") != "assistant":
                return
            texts = [
                block.get("text") or ""
                for block in payload.get("content") or []
                if isinstance(block, dict) and block.get("type") == "output_text"
            ]
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="assistant",
                    ts=ts,
                    model=self.current_model,
                    content_preview=truncate("\n".join(t for t in texts if t), PREVIEW_MAX),
                )
            )
        elif ptype == "function_call":
            name = payload.get("name") or "unknown"
            self._add_tool_call(
                ToolCallRec(
                    idx=self.tool_idx,
                    tool_name=name,
                    category=_TOOL_CATEGORY.get(name, "other"),
                    call_id=payload.get("call_id"),
                    target=_shell_target(payload.get("arguments")),
                    ts=ts,
                    input_preview=truncate(payload.get("arguments"), INPUT_PREVIEW_MAX),
                )
            )
        elif ptype == "custom_tool_call":
            name = payload.get("name") or "unknown"
            raw_input = payload.get("input")
            self._add_tool_call(
                ToolCallRec(
                    idx=self.tool_idx,
                    tool_name=name,
                    category=_TOOL_CATEGORY.get(name, "other"),
                    call_id=payload.get("call_id"),
                    ts=ts,
                    input_preview=truncate(
                        raw_input if isinstance(raw_input, str) else None, INPUT_PREVIEW_MAX
                    ),
                )
            )
        elif ptype == "function_call_output":
            call_id = payload.get("call_id")
            output = payload.get("output")
            if call_id:
                result = ToolResultRec(
                    call_id=call_id,
                    output_preview=truncate(
                        output if isinstance(output, str) else None, PREVIEW_MAX
                    ),
                )
                match = _EXEC_OUTPUT_HEADER.match(output) if isinstance(output, str) else None
                if match:
                    exit_code = int(match.group(1))
                    result.is_error = exit_code != 0
                    result.exit_code = exit_code
                    result.duration_ms = int(float(match.group(2)) * 1000)
                    result.output_bytes = _byte_len(output)
                self.chunk.tool_results.append(result)
        elif ptype == "custom_tool_call_output":
            call_id = payload.get("call_id")
            if call_id:
                self.chunk.tool_results.append(_parse_custom_tool_output(call_id, payload))
        elif ptype == "web_search_call":
            action = payload.get("action")
            query = action.get("query") if isinstance(action, dict) else None
            self._add_tool_call(
                ToolCallRec(
                    idx=self.tool_idx,
                    tool_name="web_search",
                    category="web",
                    call_id=payload.get("call_id"),
                    target=truncate(query, 200) if isinstance(query, str) else None,
                    ts=ts,
                )
            )

    def _feed_event(self, ts: str | None, payload: dict[str, Any]) -> None:
        ptype = payload.get("type")
        if ptype == "user_message":
            text = payload.get("message") or payload.get("text")
            if isinstance(text, str) and text:
                self.context.add_text(
                    event_type="message",
                    category="user_prompt",
                    text=text,
                    ts=ts,
                    role="user",
                )
                self._add_message(
                    MessageRec(
                        idx=self.msg_idx,
                        role="user",
                        ts=ts,
                        content_preview=truncate(text, USER_TEXT_MAX),
                    )
                )
        elif ptype == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            last = info.get("last_token_usage") or {}
            if not last:
                return
            total = info.get("total_token_usage") or {}
            fingerprint = _hash_text(
                json.dumps([self.context.generation, self.context.turn_id, total], sort_keys=True)
            )
            context_window = info.get("model_context_window")
            if isinstance(context_window, int):
                self.chunk.session_fields["context_window"] = context_window
            self.context.add_usage(
                ts,
                input_tokens=_int_or_none(last.get("input_tokens")),
                output_tokens=_int_or_none(last.get("output_tokens")),
                cache_read_tokens=_int_or_none(last.get("cached_input_tokens")),
                reasoning_tokens=_int_or_none(last.get("reasoning_output_tokens")),
                context_window=_int_or_none(context_window),
                metadata={
                    "provider": "codex",
                    "model": self.current_model,
                    "usage_key": fingerprint if total else None,
                    "usage_method": "cumulative_notification"
                    if total
                    else "unidentified_notification",
                },
            )
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                primary = limits.get("primary")
                secondary = limits.get("secondary")
                if isinstance(primary, dict):
                    used = _float_or_none(primary.get("used_percent"))
                    if used is not None:
                        self.chunk.session_fields["rate_limit_primary_used_percent"] = used
                if isinstance(secondary, dict):
                    used = _float_or_none(secondary.get("used_percent"))
                    if used is not None:
                        self.chunk.session_fields["rate_limit_secondary_used_percent"] = used
                plan = limits.get("plan_type")
                if isinstance(plan, str) and plan:
                    self.chunk.session_fields["rate_limit_plan"] = plan
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="event",
                    ts=ts,
                    input_tokens=_int_or_none(last.get("input_tokens")),
                    output_tokens=_int_or_none(last.get("output_tokens")),
                    cache_read_tokens=_int_or_none(last.get("cached_input_tokens")),
                    reasoning_tokens=_int_or_none(last.get("reasoning_output_tokens")),
                    extra={
                        "subtype": "token_count",
                        "total": total or None,
                        "usage_key": fingerprint if total else None,
                    },
                )
            )
        elif ptype == "exec_command_end":
            call_id = payload.get("call_id")
            if not call_id:
                return
            exit_code = _int_or_none(payload.get("exit_code"))
            output = payload.get("aggregated_output") or payload.get("stdout")
            parsed_cmd = payload.get("parsed_cmd")
            self.chunk.tool_results.append(
                ToolResultRec(
                    call_id=call_id,
                    is_error=exit_code is not None and exit_code != 0,
                    exit_code=exit_code,
                    duration_ms=_duration_to_ms(payload.get("duration")),
                    command_kind=_command_kind(parsed_cmd, payload.get("command")),
                    stdout_bytes=_byte_len(payload.get("stdout")),
                    stderr_bytes=_byte_len(payload.get("stderr")),
                    output_bytes=_byte_len(output),
                    output_preview=truncate(
                        output if isinstance(output, str) else None, PREVIEW_MAX
                    ),
                    extra={"parsed_cmd": parsed_cmd} if parsed_cmd is not None else None,
                )
            )
        elif ptype == "mcp_tool_call_end":
            call_id = payload.get("call_id")
            if not call_id:
                return
            invocation = payload.get("invocation")
            server = invocation.get("server") if isinstance(invocation, dict) else None
            tool = invocation.get("tool") if isinstance(invocation, dict) else None
            result = payload.get("result")
            is_error = False
            if isinstance(result, dict):
                # Rust Result serialization: {"Ok": {..., "is_error": bool}} | {"Err": ...}
                ok = result.get("Ok")
                is_error = "Err" in result or (isinstance(ok, dict) and ok.get("is_error") is True)
            extra = {
                key: value
                for key, value in (("mcp_server", server), ("mcp_tool", tool))
                if isinstance(value, str) and value
            }
            self.chunk.tool_results.append(
                ToolResultRec(
                    call_id=call_id,
                    is_error=is_error,
                    duration_ms=_duration_to_ms(payload.get("duration")),
                    category="mcp",
                    extra=extra or None,
                )
            )
        elif ptype == "patch_apply_end":
            call_id = payload.get("call_id")
            if not call_id:
                return
            success = payload.get("success")
            output = payload.get("stdout") or payload.get("stderr")
            changed_file_count, lines_added, lines_removed = _patch_line_counts(
                payload.get("changes")
            )
            self.chunk.tool_results.append(
                ToolResultRec(
                    call_id=call_id,
                    is_error=success is False,
                    patch_success=success if isinstance(success, bool) else None,
                    changed_file_count=changed_file_count,
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                    output_preview=truncate(
                        output if isinstance(output, str) else None, PREVIEW_MAX
                    ),
                )
            )
        elif ptype == "thread_name_updated":
            name = payload.get("thread_name")
            if isinstance(name, str) and name:
                self.chunk.session_fields["title"] = name
        elif ptype == "task_started":
            extra: dict[str, Any] = {"subtype": "task_started"}
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                extra["turn_id"] = turn_id
            self._add_message(MessageRec(idx=self.msg_idx, role="event", ts=ts, extra=extra))
        elif ptype == "task_complete":
            # Session-level turn stats are recomputed from these rows (SUM/AVG).
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="event",
                    ts=ts,
                    extra={
                        "subtype": "task_complete",
                        "duration_ms": _int_or_none(payload.get("duration_ms")),
                        "ttft_ms": _int_or_none(payload.get("time_to_first_token_ms")),
                    },
                )
            )
        elif ptype == "turn_aborted":
            extra = {"subtype": "turn_aborted"}
            reason = payload.get("reason")
            if isinstance(reason, str) and reason:
                extra["reason"] = reason
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                extra["turn_id"] = turn_id
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="system",
                    ts=ts,
                    content_preview="turn aborted",
                    extra=extra,
                )
            )
        elif ptype == "thread_rolled_back":
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="system",
                    ts=ts,
                    content_preview="thread rolled back",
                    extra={"subtype": "thread_rolled_back"},
                )
            )
        elif ptype == "context_compacted":
            self.context.add_compaction(ts, trigger="unknown", marker=True)
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="system",
                    ts=ts,
                    content_preview="context compacted",
                    extra={"subtype": "compact_boundary"},
                )
            )
        elif ptype == "error":
            extra = {"subtype": "api_error", "codex_type": "error"}
            error_info = payload.get("codex_error_info")
            if isinstance(error_info, str) and error_info:
                extra["error_info"] = error_info
            self._add_message(
                MessageRec(
                    idx=self.msg_idx,
                    role="system",
                    ts=ts,
                    content_preview=truncate(str(payload.get("message") or "error"), PREVIEW_MAX),
                    extra=extra,
                )
            )


def parse_codex_lines(
    lines: list[bytes],
    *,
    msg_idx0: int,
    tool_idx0: int,
    context_idx0: int = 0,
    model_call_idx0: int = 0,
    generation0: int = 0,
    previous_context_event: str | None = None,
) -> ParsedChunk:
    parser = _CodexParser(
        msg_idx0,
        tool_idx0,
        context_idx0,
        model_call_idx0,
        generation0,
        previous_context_event,
    )
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            parser.chunk.warnings += 1
            continue
        if not isinstance(obj, dict):
            parser.chunk.warnings += 1
            continue
        payload = obj.get("payload")
        record_type = obj.get("type")
        if not isinstance(payload, dict) or not isinstance(record_type, str):
            continue
        parser.feed(obj.get("timestamp"), record_type, payload)
    return reconcile_usage(parser.chunk)


def discover_files(root: Path) -> list[tuple[Path, str]]:
    """Find rollout files under the date-partitioned root; returns (path, session_id)."""
    found: list[tuple[Path, str]] = []
    for path in root.rglob("rollout-*.jsonl"):
        match = _ROLLOUT_FILE.search(path.name)
        if match:
            found.append((path, match.group(1)))
    found.sort(key=lambda item: str(item[0]))
    return found


def import_codex_source(
    repo: Repository,
    resolver: ProjectResolver,
    root: Path,
    machine_id: str,
    on_progress: Callable[[ImportProgress], None] | None = None,
) -> ImportStats:
    total = ImportStats()
    files = discover_files(root)
    if on_progress is not None:
        on_progress(ImportProgress(source="codex", files_done=0, files_total=len(files)))
    for files_done, (path, session_id) in enumerate(files, start=1):
        file_stats = import_stream_file(
            repo,
            resolver,
            machine_id,
            path,
            session_id,
            MAIN_STREAM,
            "codex",
            parse_codex_lines,
        )
        if file_stats is None:
            total.files_seen += 1
        else:
            total.add(file_stats)
        if on_progress is not None:
            on_progress(
                ImportProgress(source="codex", files_done=files_done, files_total=len(files))
            )
    # Subagent threads are standalone rollout files; mirror them into
    # subagent_runs on their parents once all files of this pass are in.
    total.subagent_runs += repo.sync_codex_subagent_runs(machine_id)
    return total
