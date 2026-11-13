"""Read and segment full Claude and Codex transcripts from capture or JSONL."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from agent_watchdog import readapi
from wdi.ingest.claude_code import _block_text, tool_target
from wdi.ingest.watchdog import WatchdogSource

SOURCE_ERRORS = (OSError, ValueError, KeyError, sqlite3.Error)

_PERSISTED_OUTPUT = re.compile(
    r"\A<persisted-output>\r?\nOutput too large \([^\r\n]+\)\. "
    r"Full output saved to: ([^\r\n]+)\r?\n"
)

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


def _watchdog_source(key: str, stream_id: str, data_dirs: Sequence[Path]) -> WatchdogSource | None:
    parts = key.removeprefix("wd://").split("/")
    if len(parts) != 5 or not all(parts):
        return None
    machine, project, provider, session, stored_stream = parts
    capture_stream = stream_id if provider == "claude" else stored_stream
    if provider != "claude" and stream_id != "main":
        return None

    best: WatchdogSource | None = None
    for data_dir in data_dirs:
        try:
            stores = readapi.stores(data_dir)
        except SOURCE_ERRORS:
            continue
        for store in stores:
            if not store.supported or (store.machine_id, store.project_id) != (machine, project):
                continue
            try:
                stream = next(
                    (
                        candidate
                        for candidate in readapi.streams(store)
                        if (candidate.provider, candidate.session_id, candidate.stream_id)
                        == (provider, session, capture_stream)
                    ),
                    None,
                )
            except SOURCE_ERRORS:
                continue
            if stream is not None and (
                best is None
                or (stream.generation, stream.record_count) > (best.version, best.record_count)
            ):
                best = WatchdogSource(store, stream)
    return best


@contextmanager
def source_lines(
    source_path: str | Path | None,
    stream_id: str = "main",
    *,
    data_dirs: Sequence[Path] = (),
) -> Iterator[tuple[Iterable[str | bytes], WatchdogSource | None] | None]:
    """Open the requested stream, or yield None if the source is unavailable."""
    if not source_path:
        yield None
    elif isinstance(source_path, str) and source_path.startswith("wd://"):
        source = _watchdog_source(source_path, stream_id, data_dirs)
        if source is None:
            yield None
        else:
            yield readapi.lines(source.store, source.stream.stream_pk), source
    else:
        path = stream_source_path(Path(source_path), stream_id)
        if path is None:
            yield None
        else:
            with path.open("rb") as lines:
                yield lines, None


def iter_segments(lines: Iterable[str | bytes], stream_id: str = "main") -> Iterator[Segment]:
    """Yield full-fidelity segments from one transcript stream."""
    idx = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError, UnicodeError:
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
    message = obj.get("message")
    if not isinstance(message, dict):
        return
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
    elif rtype == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
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


def _tool_output(text: str, source: WatchdogSource | None) -> str | None:
    reference = _PERSISTED_OUTPUT.match(text)
    if reference is None:
        return text
    if source is None or source.stream.provider != "claude":
        return None
    provider, session = source.stream.provider, source.stream.session_id
    path = reference[1].replace("\\", "/")
    if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        return None
    parts = path.split("/")
    if parts[-3:-1] != [session, "tool-results"] or parts[-1] in ("", ".", ".."):
        return None
    rel_path = f"tool-results/{parts[-1]}"
    try:
        side_file = max(
            (
                side
                for side in readapi.side_files(source.store, provider, session)
                if side.rel_path == rel_path
            ),
            key=lambda side: (side.captured_at, side.digest),
            default=None,
        )
        if side_file is not None:
            return readapi.side_file_bytes(
                source.store, provider, session, rel_path, side_file.digest
            ).decode("utf-8", errors="replace")
    except SOURCE_ERRORS:
        pass
    return None


def find_tool_io(
    source_path: str | Path | None,
    stream_id: str,
    call_id: str,
    *,
    data_dirs: Sequence[Path] = (),
) -> tuple[str | None, str | None]:
    """Return full tool input and output, using None for an unavailable field."""
    tool_input: str | None = None
    tool_output: str | None = None
    try:
        with source_lines(source_path, stream_id, data_dirs=data_dirs) as opened:
            if opened is None:
                return None, None
            lines, source = opened
            for seg in iter_segments(lines, stream_id):
                if seg.call_id != call_id:
                    continue
                if seg.kind == TOOL_USE:
                    tool_input = seg.text
                elif seg.kind == TOOL_RESULT:
                    tool_output = seg.text
            if tool_output is not None:
                tool_output = _tool_output(tool_output, source)
    except SOURCE_ERRORS:
        return None, None
    return tool_input, tool_output
