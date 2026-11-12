"""Privacy-preserving helpers for structural context telemetry."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from wdi.ingest.models import ContextEventRec

_WHITESPACE = re.compile(r"\s+")
_SEARCH_COMMANDS = ("rg", "grep", "findstr", "select-string")
_LIST_COMMANDS = ("ls", "dir", "get-childitem", "tree")
_READ_COMMANDS = ("cat", "type", "get-content", "head", "tail", "sed")
_VCS_COMMANDS = ("git diff", "git status", "git log", "git show")
_BUILD_COMMANDS = (
    "pytest",
    "ruff",
    "pyright",
    "mypy",
    "npm test",
    "npm run",
    "cargo test",
    "dotnet test",
    "cmake",
    "msbuild",
    "make",
    "uv run pytest",
)


def fingerprint(value: str | None) -> str | None:
    """Hash normalized text so repetitions can be detected without retaining it."""
    if not value:
        return None
    normalized = _WHITESPACE.sub(" ", value).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def target_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()


def text_sizes(value: str | None) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    return len(value), len(value.encode("utf-8"))


def json_text(value: Any) -> str | None:
    def visible(item: Any) -> Any:
        if isinstance(item, dict):
            if item.get("type") in {"image", "image_url", "input_image", "audio", "input_audio"}:
                return "[opaque media]"
            return {
                key: "[opaque media]"
                if key in {"image_url", "audio_url", "encrypted_content"}
                else visible(part)
                for key, part in item.items()
            }
        if isinstance(item, list):
            return [visible(part) for part in item]
        if isinstance(item, str):
            return re.sub(r"data:(?:image|audio|video)/[^\s\"']+", "[opaque media]", item)
        return item

    if value is None:
        return None
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(
                    visible(parsed), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
        return visible(value)
    return json.dumps(visible(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def command_kind(command: str | None) -> str | None:
    if not command:
        return None
    stripped = command.strip()
    if not stripped:
        return None
    first = stripped.split(maxsplit=1)[0]
    if (
        first.startswith(("$", "%", "&"))
        or "=" in first
        or not re.fullmatch(r"[A-Za-z0-9_./\\:-]+", first)
    ):
        return "shell_expression"
    return re.split(r"[/\\]", first)[-1].casefold()


def tool_output_category(tool_name: str, command: str | None = None) -> str:
    """Classify model-visible tool output by the operation that produced it."""
    lowered_name = tool_name.casefold().split("__")[-1].split(".")[-1]
    lowered_command = (command or "").strip().casefold()
    if "web" in lowered_name or "browser" in lowered_name:
        return "web_result"
    if lowered_name in {"read", "notebookread", "view_image", "get_code_snippet"}:
        return "code_file"
    if lowered_name in {"glob"}:
        return "directory_listing"
    if lowered_name in {"grep", "search_graph", "search_code", "find_symbol"}:
        return "search_result"
    if lowered_name in {"edit", "multiedit", "write", "notebookedit", "apply_patch"}:
        return "edit_patch"
    if lowered_name in {"exec", "wait"}:
        commands = re.findall(r"(?:cmd|command)\s*:\s*[\"\x27]([^\"\x27]+)", command or "")
        wrapped = re.findall(r"tools\.([A-Za-z0-9_]+)\s*\(", command or "")
        if not commands and command and not wrapped:
            commands = [command]
        kinds = {
            tool_output_category("shell", part.strip())
            for cmd in commands
            for part in cmd.split(";")
            if part.strip()
        }
        kinds.update(
            tool_output_category(name)
            for name in wrapped
            if name not in {"exec_command", "shell_command"}
        )
        if len(kinds) == 1:
            return kinds.pop()
        return (
            "mixed_tool_output"
            if kinds
            else "deferred_tool_output"
            if lowered_name == "wait"
            else "tool_output_other"
        )
    if lowered_name in {"bash", "powershell", "shell", "shell_command", "exec_command"}:
        parts = lowered_command.split(";")
        if len(parts) > 1:
            kinds = {tool_output_category("shell", p.strip()) for p in parts if p.strip()}
            return kinds.pop() if len(kinds) == 1 else "mixed_tool_output"
        if any(lowered_command.startswith(prefix) for prefix in _VCS_COMMANDS):
            return "vcs"
        if any(token in lowered_command for token in _BUILD_COMMANDS):
            return "build_test_log"
        if any(lowered_command.startswith(prefix) for prefix in _SEARCH_COMMANDS):
            return "directory_listing" if "--files" in lowered_command else "search_result"
        if any(lowered_command.startswith(prefix) for prefix in _LIST_COMMANDS):
            return "directory_listing"
        if any(lowered_command.startswith(prefix) for prefix in _READ_COMMANDS):
            return "code_file"
    return "tool_output_other"


class ContextTracker:
    """Assign stable event, model-call and compaction coordinates while parsing."""

    def __init__(
        self,
        events: list[ContextEventRec],
        *,
        event_idx0: int = 0,
        model_call_idx0: int = 0,
        generation0: int = 0,
        previous_event_type: str | None = None,
    ) -> None:
        self.events = events
        self.event_idx = event_idx0
        self.model_call_index = model_call_idx0
        self.generation = generation0
        self.turn_id: str | None = None
        self.previous_compaction_event = previous_event_type

    def set_turn(self, turn_id: Any) -> None:
        if isinstance(turn_id, str) and turn_id:
            self.turn_id = turn_id

    def add_text(
        self,
        *,
        event_type: str,
        category: str,
        text: str | None,
        ts: str | None,
        role: str | None = None,
        tool_name: str | None = None,
        command: str | None = None,
        target: str | None = None,
        direction: str = "input",
        slot: str | None = None,
        replacement: bool = False,
        model_call_index: int | None = None,
        call_id: str | None = None,
    ) -> None:
        if text is None:
            return
        if "[opaque media]" in text:
            self.add_opaque("opaque_media", ts, direction=direction)
            text = text.replace("[opaque media]", "")
        chars, bytes_ = text_sizes(text)
        extra: dict[str, Any] = {"direction": direction}
        extra["call_id"] = call_id
        extra["size_method"] = "visible_utf8_and_characters"
        extra["estimated_tokens"] = (len(text) + 3) // 4
        # Bound retained hashes; they support overlap diagnostics, not semantic relevance.
        extra["line_hashes"] = list(
            dict.fromkeys(
                fingerprint(line) for line in text.splitlines() if len(line.strip()) >= 16
            )
        )[:256]
        if slot:
            extra["slot"] = slot
        if replacement:
            extra["replacement"] = True
        self._append(
            ContextEventRec(
                idx=self.event_idx,
                ts=ts,
                turn_id=self.turn_id,
                model_call_index=(
                    self.model_call_index if model_call_index is None else model_call_index
                ),
                event_type=event_type,
                role=role,
                category=category,
                char_count=chars,
                byte_count=bytes_,
                fingerprint=fingerprint(text),
                tool_name=tool_name,
                command_kind=command_kind(command),
                target_fingerprint=target_fingerprint(target),
                compaction_generation=self.generation,
                extra=extra,
            )
        )

    def add_opaque(self, event_type: str, ts: str | None, *, direction: str = "output") -> None:
        self._append(
            ContextEventRec(
                idx=self.event_idx,
                ts=ts,
                turn_id=self.turn_id,
                model_call_index=self.model_call_index,
                event_type=event_type,
                category="opaque_compaction" if event_type == "opaque_compaction" else "reasoning",
                compaction_generation=self.generation,
                coverage="opaque",
                extra={"direction": direction},
            )
        )

    def add_usage(
        self,
        ts: str | None,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        context_window: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        call_index = self.model_call_index
        self._append(
            ContextEventRec(
                idx=self.event_idx,
                ts=ts,
                turn_id=self.turn_id,
                model_call_index=call_index,
                event_type="model_call",
                category="usage",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                reasoning_tokens=reasoning_tokens,
                context_window=context_window,
                compaction_generation=self.generation,
                extra=metadata,
            )
        )
        self.model_call_index += 1
        return call_index

    def add_compaction(
        self,
        ts: str | None,
        *,
        trigger: str | None = None,
        pre_tokens: int | None = None,
        post_tokens: int | None = None,
        duration_ms: int | None = None,
        payload: bool = False,
        marker: bool = False,
    ) -> bool:
        """Add one logical boundary; paired Codex marker records are not double-counted."""
        if marker and self.previous_compaction_event == "compact_payload_boundary":
            self._append(
                ContextEventRec(
                    idx=self.event_idx,
                    ts=ts,
                    turn_id=self.turn_id,
                    model_call_index=self.model_call_index,
                    event_type="compact_marker",
                    category="opaque_compaction",
                    compaction_generation=self.generation,
                    coverage="opaque",
                )
            )
            return False
        self.generation += 1
        extra = {
            key: value
            for key, value in (
                ("trigger", trigger or "unknown"),
                ("pre_tokens", pre_tokens),
                ("post_tokens", post_tokens),
                ("duration_ms", duration_ms),
                ("payload", payload),
            )
            if value is not None
        }
        self._append(
            ContextEventRec(
                idx=self.event_idx,
                ts=ts,
                turn_id=self.turn_id,
                model_call_index=self.model_call_index,
                event_type="compact_boundary",
                category="opaque_compaction",
                compaction_generation=self.generation,
                coverage="partial" if payload else "opaque",
                extra=extra,
            )
        )
        if payload:
            self.previous_compaction_event = "compact_payload_boundary"
        return True

    def _append(self, event: ContextEventRec) -> None:
        self.events.append(event)
        self.event_idx += 1
        if event.event_type in ("compact_boundary", "compact_marker"):
            self.previous_compaction_event = event.event_type
