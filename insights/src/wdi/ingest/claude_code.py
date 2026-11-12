"""Parser and importer for Claude Code session transcripts.

Layout (see docs/claude-code-session-metadata.md):
    <root>/<project-slug>/<sessionId>.jsonl              main transcript
    <root>/<project-slug>/<sessionId>/subagents/agent-<agentId>.jsonl
"""

from __future__ import annotations

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
    SubagentRec,
    ToolCallRec,
    ToolResultRec,
    truncate,
)
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.runner import import_stream_file
from wdi.ingest.usage import reconcile_usage

_AGENT_FILE = re.compile(r"^agent-([0-9a-f]+)\.jsonl$")

_TOOL_CATEGORY = {
    "Read": "read",
    "NotebookRead": "read",
    "Glob": "search",
    "Grep": "search",
    "WebSearch": "web",
    "WebFetch": "web",
    "Bash": "exec",
    "PowerShell": "exec",
    "BashOutput": "exec",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "Task": "agent",
    "Agent": "agent",
}

_SESSION_FIELD_KEYS = {
    "cwd": "cwd",
    "gitBranch": "git_branch",
    "version": "cli_version",
    "entrypoint": "entrypoint",
}

# compactMetadata / api_error keys copied into the system message extra JSON.
_COMPACT_META_KEYS = {
    "trigger": "trigger",
    "preTokens": "pre_tokens",
    "postTokens": "post_tokens",
    "durationMs": "duration_ms",
}
_API_ERROR_KEYS = {
    "apiErrorStatus": "api_error_status",
    "retryAttempt": "retry_attempt",
    "maxRetries": "max_retries",
    "retryInMs": "retry_in_ms",
}


def tool_category(name: str) -> str:
    return _TOOL_CATEGORY.get(name, "other")


def tool_target(name: str, tool_input: dict[str, Any]) -> str | None:
    if name in ("Read", "Edit", "MultiEdit", "Write", "NotebookRead", "NotebookEdit"):
        return tool_input.get("file_path")
    if name in ("Glob", "Grep"):
        return tool_input.get("pattern")
    if name in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        return truncate(command, 200) if isinstance(command, str) else None
    if name in ("WebSearch", "WebFetch"):
        return tool_input.get("query") or tool_input.get("url")
    if name == "Task":
        return tool_input.get("subagent_type")
    return None


def _block_text(content: Any) -> str:
    """Extract plain text from a content value: str or list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _sum_existing(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _parse_assistant(
    obj: dict[str, Any],
    chunk: ParsedChunk,
    context: ContextTracker,
    msg_idx: int,
    tool_idx: int,
) -> int:
    """Append the assistant message and its tool_use calls; returns new tool_idx."""
    message = obj.get("message") or {}
    usage = message.get("usage") or {}
    ts = obj.get("timestamp")
    content = message.get("content")
    cache_creation = usage.get("cache_creation") or {}
    cache_5m = _int_or_none(cache_creation.get("ephemeral_5m_input_tokens"))
    cache_1h = _int_or_none(cache_creation.get("ephemeral_1h_input_tokens"))
    iterations = usage.get("iterations")
    iteration_count = len(iterations) if isinstance(iterations, list) else None
    server_tool_use = usage.get("server_tool_use") or {}
    web_search = _int_or_none(server_tool_use.get("web_search_requests"))
    web_fetch = _int_or_none(server_tool_use.get("web_fetch_requests"))
    accumulated: list[tuple[str, int | None]] = [
        ("cache_creation_5m_tokens", cache_5m),
        ("cache_creation_1h_tokens", cache_1h),
        ("usage_iteration_count", iteration_count),
        ("web_search_requests", web_search),
        ("web_fetch_requests", web_fetch),
    ]
    for key, value in accumulated:
        total = _sum_existing(_int_or_none(chunk.session_fields.get(key)), value)
        if total is not None:
            chunk.session_fields[key] = total
    texts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                tool_uses.append(block)
    elif isinstance(content, str):
        texts.append(content)

    call_index = context.add_usage(
        ts,
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
        metadata={
            "provider": "claude",
            "model": message.get("model"),
            "usage_key": message.get("id"),
            "usage_method": "provider_message_id"
            if message.get("id")
            else "unidentified_notification",
            "cache_creation": usage.get("cache_creation"),
        },
    )
    for text in texts:
        context.add_text(
            event_type="message",
            category="assistant_text",
            text=text,
            ts=ts,
            role="assistant",
            direction="output",
            model_call_index=call_index,
        )

    chunk.messages.append(
        MessageRec(
            idx=msg_idx,
            role="assistant",
            ts=ts,
            model=message.get("model"),
            message_id=obj.get("uuid"),
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
            cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
            cache_creation_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
            stop_reason=message.get("stop_reason")
            if isinstance(message.get("stop_reason"), str)
            else None,
            content_preview=truncate("\n".join(t for t in texts if t), PREVIEW_MAX),
            extra={
                "usage_key": message.get("id"),
                "cache_creation_5m_tokens": cache_5m,
                "cache_creation_1h_tokens": cache_1h,
                "usage_iteration_count": iteration_count,
                "web_search_requests": web_search,
                "web_fetch_requests": web_fetch,
            },
        )
    )
    for block in tool_uses:
        name = block.get("name") or "unknown"
        raw_input = block.get("input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else None
        target = tool_target(name, tool_input)
        context.add_text(
            event_type="tool_input",
            category="tool_input",
            text=json_text(tool_input),
            ts=ts,
            role="assistant",
            tool_name=name,
            command=command,
            target=target,
            direction="output",
            model_call_index=call_index,
        )
        chunk.tool_calls.append(
            ToolCallRec(
                idx=tool_idx,
                tool_name=name,
                category=tool_category(name),
                call_id=block.get("id"),
                target=target,
                ts=ts,
                input_preview=truncate(
                    json.dumps(tool_input, ensure_ascii=False), INPUT_PREVIEW_MAX
                ),
            )
        )
        tool_idx += 1
    return tool_idx


def _parse_user(
    obj: dict[str, Any], chunk: ParsedChunk, context: ContextTracker, msg_idx: int
) -> bool:
    """Handle a user record; returns True when a message row was appended."""
    tool_use_result = obj.get("toolUseResult")
    if isinstance(tool_use_result, dict) and "agentId" in tool_use_result:
        stats = tool_use_result.get("toolStats") or {}
        chunk.subagents.append(
            SubagentRec(
                agent_run_id=tool_use_result["agentId"],
                agent_type=tool_use_result.get("agentType"),
                prompt=tool_use_result.get("prompt"),
                status=tool_use_result.get("status"),
                total_duration_ms=_int_or_none(tool_use_result.get("totalDurationMs")),
                total_tokens=_int_or_none(tool_use_result.get("totalTokens")),
                total_tool_use_count=_int_or_none(tool_use_result.get("totalToolUseCount")),
                read_count=_int_or_none(stats.get("readCount")),
                search_count=_int_or_none(stats.get("searchCount")),
                bash_count=_int_or_none(stats.get("bashCount")),
                edit_file_count=_int_or_none(stats.get("editFileCount")),
                lines_added=_int_or_none(stats.get("linesAdded")),
                lines_removed=_int_or_none(stats.get("linesRemoved")),
                extra={
                    "usage": tool_use_result.get("usage"),
                    "source_tool_assistant_uuid": tool_use_result.get("sourceToolAssistantUUID"),
                },
            )
        )

    message = obj.get("message") or {}
    content = message.get("content")
    had_results = False
    texts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if call_id:
                    output_text = _block_text(block.get("content"))
                    call = next(
                        (item for item in reversed(chunk.tool_calls) if item.call_id == call_id),
                        None,
                    )
                    context.add_text(
                        event_type="tool_output",
                        category=tool_output_category(
                            call.tool_name if call else "unknown",
                            call.target if call and call.category == "exec" else None,
                        ),
                        text=output_text,
                        ts=obj.get("timestamp"),
                        role="tool",
                        tool_name=call.tool_name if call else None,
                        call_id=call_id,
                        command=call.target if call and call.category == "exec" else None,
                        target=call.target if call else None,
                    )
                    chunk.tool_results.append(
                        ToolResultRec(
                            call_id=call_id,
                            is_error=bool(block.get("is_error")),
                            ts=obj.get("timestamp"),
                            output_bytes=len(output_text.encode("utf-8")),
                            output_preview=truncate(output_text, PREVIEW_MAX),
                        )
                    )
                had_results = True
            elif block.get("type") == "text":
                texts.append(block.get("text") or "")
    elif isinstance(content, str):
        texts.append(content)

    text = "\n".join(t for t in texts if t)
    if text and not had_results and not obj.get("isMeta") and not obj.get("isCompactSummary"):
        context.set_turn(obj.get("uuid") or f"user:{msg_idx}")
        context.add_text(
            event_type="message",
            category="user_prompt",
            text=text,
            ts=obj.get("timestamp"),
            role="user",
        )
    if had_results or not text or obj.get("isMeta") or obj.get("isCompactSummary"):
        return False
    chunk.messages.append(
        MessageRec(
            idx=msg_idx,
            role="user",
            ts=obj.get("timestamp"),
            message_id=obj.get("uuid"),
            content_preview=truncate(text, USER_TEXT_MAX),
        )
    )
    return True


def parse_claude_lines(
    lines: list[bytes],
    *,
    msg_idx0: int,
    tool_idx0: int,
    context_idx0: int = 0,
    model_call_idx0: int = 0,
    generation0: int = 0,
    previous_context_event: str | None = None,
) -> ParsedChunk:
    chunk = ParsedChunk()
    context = ContextTracker(
        chunk.context_events,
        event_idx0=context_idx0,
        model_call_idx0=model_call_idx0,
        generation0=generation0,
        previous_event_type=previous_context_event,
    )
    msg_idx = msg_idx0
    tool_idx = tool_idx0
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            chunk.warnings += 1
            continue
        if not isinstance(obj, dict):
            chunk.warnings += 1
            continue

        for src_key, dst_key in _SESSION_FIELD_KEYS.items():
            value = obj.get(src_key)
            if value and dst_key not in chunk.session_fields:
                chunk.session_fields[dst_key] = value

        record_type = obj.get("type")
        if record_type == "assistant":
            tool_idx = _parse_assistant(obj, chunk, context, msg_idx, tool_idx)
            msg_idx += 1
        elif record_type == "user":
            if _parse_user(obj, chunk, context, msg_idx):
                msg_idx += 1
        elif record_type == "system":
            subtype = obj.get("subtype")
            if subtype == "stop_hook_summary":
                hook_count = _int_or_none(obj.get("hookCount")) or 0
                hook_errors = obj.get("hookErrors")
                error_count = len(hook_errors) if isinstance(hook_errors, list) else 0
                infos = obj.get("hookInfos")
                commands: list[str] = []
                if isinstance(infos, list):
                    for info in infos:
                        if isinstance(info, dict) and isinstance(info.get("command"), str):
                            commands.append(info["command"])
                existing_commands = chunk.session_fields.get("hook_commands")
                combined_commands = [
                    *(
                        existing_commands.split("\n")
                        if isinstance(existing_commands, str) and existing_commands
                        else []
                    ),
                    *commands,
                ]
                chunk.session_fields["hook_count"] = (
                    _int_or_none(chunk.session_fields.get("hook_count")) or 0
                ) + hook_count
                chunk.session_fields["hook_error_count"] = (
                    _int_or_none(chunk.session_fields.get("hook_error_count")) or 0
                ) + error_count
                if combined_commands:
                    chunk.session_fields["hook_commands"] = "\n".join(combined_commands)
                if obj.get("hasOutput") is True:
                    chunk.session_fields["hook_has_output_count"] = (
                        _int_or_none(chunk.session_fields.get("hook_has_output_count")) or 0
                    ) + 1
            if subtype in ("compact_boundary", "api_error"):
                extra: dict[str, Any] = {"subtype": subtype}
                if subtype == "compact_boundary":
                    meta = obj.get("compactMetadata")
                    if isinstance(meta, dict):
                        for src, dst in _COMPACT_META_KEYS.items():
                            value = meta.get(src)
                            if isinstance(value, (str, int)):
                                extra[dst] = value
                    context.add_compaction(
                        obj.get("timestamp"),
                        trigger=(
                            extra.get("trigger") if isinstance(extra.get("trigger"), str) else None
                        ),
                        pre_tokens=_int_or_none(extra.get("pre_tokens")),
                        post_tokens=_int_or_none(extra.get("post_tokens")),
                        duration_ms=_int_or_none(extra.get("duration_ms")),
                    )
                else:
                    for src, dst in _API_ERROR_KEYS.items():
                        value = obj.get(src)
                        if isinstance(value, (str, int)):
                            extra[dst] = value
                chunk.messages.append(
                    MessageRec(
                        idx=msg_idx,
                        role="system",
                        ts=obj.get("timestamp"),
                        message_id=obj.get("uuid"),
                        content_preview=truncate(str(obj.get("error") or subtype), PREVIEW_MAX),
                        extra=extra,
                    )
                )
                msg_idx += 1
        elif record_type == "ai-title":
            title = obj.get("aiTitle")
            if isinstance(title, str) and title:
                chunk.session_fields["title"] = title
        # attachment / last-prompt / queue-operation carry no metrics: skipped
    return reconcile_usage(chunk)


def discover_files(root: Path) -> list[tuple[Path, str, str]]:
    """Find transcripts under root; returns (path, session_id, stream_id).

    Main transcripts sort before subagent files so the parent session row
    exists by the time its subagent streams are imported.
    """
    found: list[tuple[Path, str, str]] = []
    for path in root.rglob("*.jsonl"):
        match = _AGENT_FILE.match(path.name)
        if match and path.parent.name == "subagents":
            session_id = path.parent.parent.name
            found.append((path, session_id, match.group(1)))
        elif path.parent.parent == root:
            found.append((path, path.stem, MAIN_STREAM))
    found.sort(key=lambda item: (item[2] != MAIN_STREAM, str(item[0])))
    return found


def import_claude_source(
    repo: Repository,
    resolver: ProjectResolver,
    root: Path,
    machine_id: str,
    on_progress: Callable[[ImportProgress], None] | None = None,
) -> ImportStats:
    total = ImportStats()
    files = discover_files(root)
    if on_progress is not None:
        on_progress(ImportProgress(source="claude", files_done=0, files_total=len(files)))
    for files_done, (path, session_id, stream_id) in enumerate(files, start=1):
        file_stats = import_stream_file(
            repo,
            resolver,
            machine_id,
            path,
            session_id,
            stream_id,
            "claude",
            lambda lines, m0, t0, c0, mc0, g0, previous: parse_claude_lines(
                lines,
                msg_idx0=m0,
                tool_idx0=t0,
                context_idx0=c0,
                model_call_idx0=mc0,
                generation0=g0,
                previous_context_event=previous,
            ),
        )
        if file_stats is None:
            total.files_seen += 1
        else:
            total.add(file_stats)
        if on_progress is not None:
            on_progress(
                ImportProgress(source="claude", files_done=files_done, files_total=len(files))
            )
    return total
