"""Normalized records produced by parsers, agent-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Full text is kept for user prompts (LLM analyzers need it); other content is previewed.
USER_TEXT_MAX = 10_000
PREVIEW_MAX = 500
INPUT_PREVIEW_MAX = 300

MAIN_STREAM = "main"


def truncate(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[:limit]


@dataclass(slots=True)
class MessageRec:
    idx: int
    role: str
    ts: str | None = None
    model: str | None = None
    message_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    stop_reason: str | None = None
    content_preview: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolCallRec:
    idx: int
    tool_name: str
    category: str
    call_id: str | None = None
    target: str | None = None
    ts: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    is_error: bool = False
    command_kind: str | None = None
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    output_bytes: int | None = None
    patch_success: bool | None = None
    changed_file_count: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolResultRec:
    """Tool outcome reported separately from the call; correlated by call_id.

    is_error=None means "don't change": Codex reports the result and the exec
    telemetry as separate events, and a later event must not clobber the flag.
    exit_code and duration_ms are first-writer-wins on apply: the exec event
    (precise, arrives first) beats the coarser header parsed from the model-
    visible output.
    """

    call_id: str
    is_error: bool | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    # Category override (e.g. "mcp" from mcp_tool_call_end); None keeps the
    # category assigned at call time.
    category: str | None = None
    # Result timestamp; when duration_ms is absent, latency is derived as
    # the delta from the originating call's ts (Claude has no explicit duration).
    ts: str | None = None
    output_preview: str | None = None
    command_kind: str | None = None
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    output_bytes: int | None = None
    patch_success: bool | None = None
    changed_file_count: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class ContextEventRec:
    """Structural context event; content itself is never persisted here."""

    idx: int
    event_type: str
    category: str
    ts: str | None = None
    turn_id: str | None = None
    model_call_index: int | None = None
    role: str | None = None
    char_count: int | None = None
    byte_count: int | None = None
    fingerprint: str | None = None
    tool_name: str | None = None
    command_kind: str | None = None
    target_fingerprint: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    context_window: int | None = None
    compaction_generation: int = 0
    coverage: str = "visible"
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class SubagentRec:
    agent_run_id: str
    agent_type: str | None = None
    prompt: str | None = None
    status: str | None = None
    total_duration_ms: int | None = None
    total_tokens: int | None = None
    total_tool_use_count: int | None = None
    read_count: int | None = None
    search_count: int | None = None
    bash_count: int | None = None
    edit_file_count: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class ParsedChunk:
    """Result of parsing a contiguous chunk of JSONL lines from one stream."""

    messages: list[MessageRec] = field(default_factory=list)
    tool_calls: list[ToolCallRec] = field(default_factory=list)
    tool_results: list[ToolResultRec] = field(default_factory=list)
    context_events: list[ContextEventRec] = field(default_factory=list)
    subagents: list[SubagentRec] = field(default_factory=list)
    session_fields: dict[str, str | int | float] = field(default_factory=dict)
    warnings: int = 0


@dataclass(slots=True)
class ImportStats:
    files_seen: int = 0
    files_imported: int = 0
    sessions: int = 0
    messages: int = 0
    tool_calls: int = 0
    context_events: int = 0
    subagent_runs: int = 0
    warnings: int = 0

    def add(self, other: ImportStats) -> None:
        self.files_seen += other.files_seen
        self.files_imported += other.files_imported
        self.sessions += other.sessions
        self.messages += other.messages
        self.tool_calls += other.tool_calls
        self.context_events += other.context_events
        self.subagent_runs += other.subagent_runs
        self.warnings += other.warnings


@dataclass(frozen=True, slots=True)
class ImportProgress:
    source: str
    files_done: int
    files_total: int
