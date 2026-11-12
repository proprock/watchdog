"""Read-only segmenter over original Claude transcripts.

Parses a session's source JSONL at full fidelity (no truncation) into typed
segments. Powers both per-analyzer LLM context (sieve by kind, cap by budget) and
the web drill-down's on-demand full tool I/O. Codex is not segmented yet; callers
fall back to stored previews for those sessions.

All functions are defensive: a missing or malformed file yields nothing rather
than raising into a request or an analysis run.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wdi.ingest.claude_code import _block_text, tool_target

# Segment kinds.
USER_PROMPT = "user_prompt"
ASSISTANT_TEXT = "assistant_text"
REASONING = "reasoning"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class Segment:
    kind: str
    text: str
    idx: int
    stream_id: str = "main"
    tool_name: str | None = None
    call_id: str | None = None
    target: str | None = None
    is_error: bool = False


def stream_source_path(main_source: Path, stream_id: str) -> Path | None:
    """Resolve the JSONL file backing a stream: main transcript or a subagent file."""
    if stream_id == "main":
        return main_source if main_source.is_file() else None
    # Subagent files live in <sessionId>/subagents/agent-<agentId>.jsonl next to main.
    subagent = main_source.with_suffix("") / "subagents" / f"agent-{stream_id}.jsonl"
    return subagent if subagent.is_file() else None


def iter_segments(path: Path, stream_id: str = "main") -> Iterator[Segment]:
    """Yield full-fidelity segments from one Claude transcript file."""
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    idx = 0
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            for seg in _record_segments(obj, idx, stream_id):
                yield seg
                idx += 1


def _record_segments(obj: dict[str, Any], idx: int, stream_id: str) -> Iterator[Segment]:
    rtype = obj.get("type")
    payload = obj.get("payload")
    if isinstance(payload, dict):
        yield from _codex_record_segments(rtype, payload, idx, stream_id)
        return
    message = obj.get("message") or {}
    content = message.get("content")
    if rtype == "assistant":
        yield from _assistant_segments(content, idx, stream_id)
    elif rtype == "user":
        yield from _user_segments(content, idx, stream_id)


def _codex_record_segments(
    rtype: str | None, payload: dict[str, Any], idx: int, stream_id: str
) -> Iterator[Segment]:
    ptype = payload.get("type")
    if rtype == "event_msg" and ptype == "user_message":
        text = payload.get("message") or payload.get("text")
        if isinstance(text, str) and text.strip():
            yield Segment(USER_PROMPT, text, idx, stream_id)
    elif rtype == "event_msg" and ptype == "agent_message":
        text = payload.get("message")
        if isinstance(text, str) and text.strip():
            yield Segment(ASSISTANT_TEXT, text, idx, stream_id)
    elif rtype == "response_item" and ptype == "message":
        if payload.get("role") != "assistant":
            return
        texts = [
            block.get("text") or ""
            for block in payload.get("content") or []
            if isinstance(block, dict) and block.get("type") == "output_text"
        ]
        text = "\n".join(t for t in texts if t)
        if text.strip():
            yield Segment(ASSISTANT_TEXT, text, idx, stream_id)
    elif rtype == "response_item" and ptype in ("function_call", "custom_tool_call"):
        name = payload.get("name") or "unknown"
        raw_input = payload.get("arguments") if ptype == "function_call" else payload.get("input")
        text = (
            raw_input if isinstance(raw_input, str) else json.dumps(raw_input, ensure_ascii=False)
        )
        yield Segment(
            TOOL_USE,
            text,
            idx,
            stream_id,
            tool_name=name,
            call_id=payload.get("call_id"),
        )
    elif rtype == "response_item" and ptype == "function_call_output":
        output = payload.get("output")
        if isinstance(output, str):
            yield Segment(TOOL_RESULT, output, idx, stream_id, call_id=payload.get("call_id"))


def _assistant_segments(content: Any, idx: int, stream_id: str) -> Iterator[Segment]:
    if isinstance(content, str):
        if content.strip():
            yield Segment(ASSISTANT_TEXT, content, idx, stream_id)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            yield Segment(ASSISTANT_TEXT, block["text"], idx, stream_id)
        elif btype == "thinking" and block.get("thinking"):
            yield Segment(REASONING, block["thinking"], idx, stream_id)
        elif btype == "tool_use":
            name = block.get("name") or "unknown"
            raw = block.get("input")
            tool_input: dict[str, Any] = raw if isinstance(raw, dict) else {}
            yield Segment(
                TOOL_USE,
                json.dumps(tool_input, ensure_ascii=False, indent=2),
                idx,
                stream_id,
                tool_name=name,
                call_id=block.get("id"),
                target=tool_target(name, tool_input),
            )


def _user_segments(content: Any, idx: int, stream_id: str) -> Iterator[Segment]:
    if isinstance(content, str):
        if content.strip():
            yield Segment(USER_PROMPT, content, idx, stream_id)
        return
    if not isinstance(content, list):
        return
    texts: list[str] = []
    saw_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            saw_result = True
            yield Segment(
                TOOL_RESULT,
                _block_text(block.get("content")),
                idx,
                stream_id,
                call_id=block.get("tool_use_id"),
                is_error=bool(block.get("is_error")),
            )
        elif block.get("type") == "text" and block.get("text"):
            texts.append(block["text"])
    text = "\n".join(texts)
    # A genuine user prompt has text and no tool results (those are tool plumbing).
    if text.strip() and not saw_result:
        yield Segment(USER_PROMPT, text, idx, stream_id)


def find_tool_io(main_source: Path, stream_id: str, call_id: str) -> tuple[str | None, str | None]:
    """Return (full_input, full_output) for one tool call, read from source."""
    path = stream_source_path(main_source, stream_id)
    if path is None:
        return None, None
    tool_input: str | None = None
    tool_output: str | None = None
    for seg in iter_segments(path, stream_id):
        if seg.call_id != call_id:
            continue
        if seg.kind == TOOL_USE:
            tool_input = seg.text
        elif seg.kind == TOOL_RESULT:
            tool_output = seg.text
    return tool_input, tool_output
