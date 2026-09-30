"""Version-gated, daemon-only transcript enrichment for Codex and Claude.

Codex uses the ``codex-rollout-v1`` reader (cumulative ``thread_token_usage``
deltas). Claude uses the ``claude-transcript-v1`` reader: each assistant
response carries its own ``message.usage``, repeated on every content block of
the response, and can grow across blocks while the response streams (commonly
seen on subagent responses). One ``usage`` event is emitted per distinct
``requestId``, holding the response open until a block reports a non-null
``stop_reason`` so the emitted value is the final one, not the first. Both
readers share the file-identity, offset, partial-tail, rotation and resume
machinery below, and neither emits a response already stored for the same
conversation agent, under any event id.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, cast
from uuid import NAMESPACE_URL, uuid5

from agent_watchdog.events import Availability, Envelope
from agent_watchdog.storage import RejectedEvent, StorageError

if TYPE_CHECKING:
    from agent_watchdog.storage import Store


READER = "codex-rollout-v1"
CLAUDE_READER = "claude-transcript-v1"
READ_BYTES = 1024**2
# One line is read whole up to this size, even across read windows. The longest
# real Codex rollout line seen locally is 6.7 MiB (an item_completed event_msg);
# the former 1 MiB limit stopped such sources for good and left every later
# usage record unread. A longer line is skipped with a gap, never silently.
MAX_LINE_BYTES = 32 * 1024**2
COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
# Grammar locked to Claude Code 2.1.259 / 2.1.260 (read-only inspection of real
# local transcripts). ``message.usage`` reports no total; never synthesise one.
CLAUDE_COUNTERS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "thinking_tokens",
)
CLAUDE_OBSERVED_KEYS = ("input_tokens", "cache_read_input_tokens", "output_tokens")
# Subagent reader rows pack the parent conversation id and the subagent's own
# agent id into the ``session_id`` column as ``<parent>#<agent_id>`` so no schema
# bump is needed before WD-111 owns the v6 migration.
SUBAGENT_SEPARATOR = "#"
_THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

TranscriptFailureCode = Literal[
    "rollout_line_invalid",
    "rollout_record_invalid",
    "session_meta_mismatch",
    "session_meta_missing",
    "session_meta_version_missing",
    "thread_usage_invalid",
    "thread_usage_missing",
    "thread_usage_total_missing",
    "transcript_source_invalid",
    "transcript_sources_unavailable",
    "transcript_path_not_regular",
    "transcript_unreadable",
    "usage_record_invalid",
    "usage_response_missing",
    "usage_session_mismatch",
    "usage_counter_reset",
    "transcript_io_unavailable",
    "transcript_storage_unavailable",
    "transcript_enrichment_invalid",
    "claude_line_invalid",
    "claude_record_invalid",
    "claude_session_mismatch",
    "claude_usage_invalid",
    "claude_response_oversized",
    "transcript_line_oversized",
    "subagent_meta_mismatch",
]
FAILURE_CODES: frozenset[str] = frozenset(
    {
        "rollout_line_invalid",
        "rollout_record_invalid",
        "session_meta_mismatch",
        "session_meta_missing",
        "session_meta_version_missing",
        "thread_usage_invalid",
        "thread_usage_missing",
        "thread_usage_total_missing",
        "claude_line_invalid",
        "claude_record_invalid",
        "claude_session_mismatch",
        "claude_usage_invalid",
        "claude_response_oversized",
        "transcript_line_oversized",
        "subagent_meta_mismatch",
        "transcript_source_invalid",
        "transcript_sources_unavailable",
        "transcript_path_not_regular",
        "transcript_unreadable",
        "usage_record_invalid",
        "usage_response_missing",
        "usage_session_mismatch",
        "usage_counter_reset",
        "transcript_io_unavailable",
        "transcript_storage_unavailable",
        "transcript_enrichment_invalid",
    }
)


@dataclass(frozen=True)
class TranscriptSource:
    provider: str
    session_id: str
    path: str


@dataclass(frozen=True)
class EnrichmentResult:
    accepted: int = 0
    failures: tuple[TranscriptFailureCode, ...] = ()
    active_failures: tuple[TranscriptFailureCode, ...] = ()
    # New usage-bearing lines were read but none were stored, and no failure was
    # raised: a silent drop the daemon must surface rather than treat as idle.
    inert: bool = False

    def __bool__(self) -> bool:
        """Report activity only when new usage observations were accepted."""
        return self.accepted > 0


class UnsupportedTranscript(ValueError):
    def __init__(self, code: TranscriptFailureCode) -> None:
        if code not in FAILURE_CODES:
            code = "transcript_source_invalid"
        self.code = code
        super().__init__(code)


def failure_code(error: OSError | StorageError | ValueError) -> TranscriptFailureCode:
    """Classify a contained daemon-side enrichment exception without its text."""
    if isinstance(error, StorageError):
        return "transcript_storage_unavailable"
    if isinstance(error, OSError):
        return "transcript_io_unavailable"
    return "transcript_enrichment_invalid"


def _hook_path(payload: object, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    # Codex reports some rollout paths in the Win32 verbatim form (``\\?\C:\...``,
    # seen on SessionEnd and in resumed sessions) and others plainly. Strip only
    # that prefix so both spellings key one source; wider normalisation would
    # re-key sources already stored under their literal spelling.
    if value.startswith("\\\\?\\") and value[5:7] == ":\\":
        value = value[4:]
    path = Path(value)
    return str(path) if path.is_absolute() else None


def sources_from_hook(event: Envelope) -> tuple[TranscriptSource, ...]:
    """Return the durable reader sources a hook envelope registers.

    A Codex or Claude hook with an absolute ``transcript_path`` registers the
    session transcript. A Claude ``agent.end`` hook with an absolute
    ``agent_transcript_path`` additionally registers the subagent transcript,
    keyed by ``<parent session>#<agent id>`` so the emitted usage rows resolve to
    the parent conversation and the subagent's ``agent_id``.
    """
    if event.source != "hook" or not event.session_id:
        return ()
    if event.provider not in ("codex", "claude"):
        return ()
    payload = event.payload.get(event.provider)
    sources: list[TranscriptSource] = []
    session_path = _hook_path(payload, "transcript_path")
    if session_path is not None:
        sources.append(TranscriptSource(event.provider, event.session_id, session_path))
    if event.provider == "claude" and event.kind == "agent.end" and event.agent_id:
        agent_path = _hook_path(payload, "agent_transcript_path")
        if agent_path is not None:
            keyed = f"{event.session_id}{SUBAGENT_SEPARATOR}{event.agent_id}"
            sources.append(TranscriptSource("claude", keyed, agent_path))
    return tuple(sources)


def _signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    if not path.is_file() or path.is_symlink():
        raise UnsupportedTranscript("transcript_path_not_regular")
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _counters(value: object) -> dict[str, int | None]:
    if not isinstance(value, dict):
        raise UnsupportedTranscript("thread_usage_missing")
    result: dict[str, int | None] = {}
    for name in COUNTERS:
        item = value.get(name)
        if item is not None and (type(item) is not int or item < 0):
            raise UnsupportedTranscript("thread_usage_invalid")
        result[name] = item
    if result["total_tokens"] is None:
        raise UnsupportedTranscript("thread_usage_total_missing")
    return result


def _usage_record(record: object, session_id: str) -> tuple[dict[str, Any], dict[str, int | None]]:
    if not isinstance(record, dict) or record.get("type") != "token_usage_record":
        raise UnsupportedTranscript("usage_record_invalid")
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("session_id") != session_id:
        raise UnsupportedTranscript("usage_session_mismatch")
    response_id = payload.get("response_id")
    if not isinstance(response_id, str) or not response_id:
        raise UnsupportedTranscript("usage_response_missing")
    counters = _counters(payload.get("thread_token_usage"))
    return payload, counters


def _rollout_agent(source: Mapping[str, object]) -> str | None:
    """The subagent thread a Codex rollout belongs to, or None for the session's own.

    Codex names a rollout ``rollout-<time>-<thread id>.jsonl``. A hook fired
    inside a subagent reports the subagent's own rollout under the parent
    session id, so its thread id differs from the session; ``_meta_record``
    verifies that thread before any of its usage is trusted.
    """
    match = _THREAD_ID.search(Path(str(source["path"])).stem)
    thread = match.group(0) if match else None
    return thread if thread is not None and thread != source["session_id"] else None


def _meta_record(record: object, session_id: str, agent_id: str | None) -> None:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise UnsupportedTranscript("session_meta_missing")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise UnsupportedTranscript("session_meta_mismatch")
    if agent_id is None:
        if payload.get("id") != session_id:
            raise UnsupportedTranscript("session_meta_mismatch")
    elif payload.get("id") != agent_id or payload.get("session_id") != session_id:
        # A subagent thread names itself and the session that spawned it.
        raise UnsupportedTranscript("subagent_meta_mismatch")
    if not isinstance(payload.get("cli_version"), str):
        raise UnsupportedTranscript("session_meta_version_missing")


def _gap(store: "Store", source: dict[str, object], reason: str, signature: str) -> None:
    provider = str(source.get("provider") or "codex")
    reader = CLAUDE_READER if provider == "claude" else READER
    session_id, _ = _split_subagent(source)
    event_id = uuid5(
        NAMESPACE_URL,
        f"watchdog|{reader}|gap|{store.project_id}|{source['session_id']}|{source['path']}|{reason}|{signature}",
    )
    event = Envelope(
        event_id=event_id,
        provider=provider,
        project_id=store.project_id,
        session_id=session_id,
        native_event_id=f"transcript-gap:{event_id.hex}",
        kind="observation.gap",
        source="transcript",
        payload={provider: {"reader": reader, "reason": reason}},
        availability={
            "usage": "unavailable",
            "input_tokens": "unavailable",
            "output_tokens": "unavailable",
            "cached_input_tokens": "unavailable",
        },
    )
    store.put(event)


def _availability(counters: dict[str, int | None]) -> dict[str, Availability]:
    return {
        name: "observed" if counters[name] is not None else "unavailable"
        for name in ("input_tokens", "output_tokens", "cached_input_tokens")
    }


def _delta(
    previous: dict[str, int | None] | None, current: dict[str, int | None]
) -> tuple[dict[str, int | None], bool]:
    if previous is None:
        return current.copy(), False
    reset = False
    for name in COUNTERS:
        before, after = previous[name], current[name]
        if before is not None and after is not None and after < before:
            reset = True
            break
    if reset:
        return {name: None for name in COUNTERS}, True
    delta: dict[str, int | None] = {}
    for name in COUNTERS:
        before, after = previous[name], current[name]
        delta[name] = after - before if before is not None and after is not None else None
    return delta, False


def _usage_event(
    store: "Store",
    source: dict[str, object],
    record: dict[str, Any],
    counters: dict[str, int | None],
    delta: dict[str, int | None],
) -> Envelope:
    payload = record["payload"]
    occurred_at = _occurred_at(record.get("timestamp"))
    agent_id = _rollout_agent(source)
    # One response is one usage event per conversation agent, however the
    # rollout path is spelled and wherever its line sits in the file.
    name = f"{READER}|{source['session_id']}|{agent_id or ''}|{payload['response_id']}"
    event_id = uuid5(NAMESPACE_URL, name)
    return Envelope(
        event_id=event_id,
        provider="codex",
        project_id=store.project_id,
        session_id=str(source["session_id"]),
        agent_id=agent_id,
        turn_id=payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None,
        native_event_id=str(payload["response_id"]),
        kind="usage",
        source="transcript",
        occurred_at=occurred_at,
        payload={
            "codex": {
                "reader": READER,
                "response_id": payload["response_id"],
                "thread_id": payload.get("thread_id")
                if isinstance(payload.get("thread_id"), str)
                else None,
                "usage": {"cumulative": counters, "delta": delta},
            }
        },
        availability=_availability(counters),
    )


def _occurred_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate if candidate.tzinfo is not None else None


def _split_subagent(source: Mapping[str, object]) -> tuple[str, str | None]:
    """Return ``(parent_session_id, agent_id)`` for a Claude reader row."""
    raw = str(source["session_id"])
    if source.get("provider") == "claude" and SUBAGENT_SEPARATOR in raw:
        parent, agent_id = raw.split(SUBAGENT_SEPARATOR, 1)
        return parent, (agent_id or None)
    return raw, None


@dataclass(frozen=True)
class _ClaudeResponse:
    request_id: str
    counters: dict[str, int | None]
    model: str | None
    version: str | None
    occurred_at: datetime | None
    stop_reason: str | None


def _has_usage(record: object) -> bool:
    """A line that reports token usage, whether or not the reader accepts it."""
    return (
        isinstance(record, dict)
        and record.get("type") == "assistant"
        and isinstance(record.get("message"), dict)
        and isinstance(record["message"].get("usage"), dict)
    )


def _claude_response(
    record: object, expected_session: str | None, *, allow_sidechain: bool = False
) -> _ClaudeResponse | None:
    """Interpret one Claude transcript line, or ``None`` to skip it.

    ``expected_session`` is the parent conversation id for a session transcript;
    for a subagent transcript it is ``None`` until the first usable line binds the
    child's own ``sessionId`` and later lines are checked for consistency.

    ``allow_sidechain`` keeps ``isSidechain`` lines: in a dedicated subagent
    transcript every line carries that flag and is the subagent's own work, so
    the skip that de-duplicates it out of the *parent* transcript must not apply.
    """
    if not isinstance(record, dict):
        raise UnsupportedTranscript("claude_record_invalid")
    if record.get("type") != "assistant":
        return None
    if record.get("isSidechain") is True and not allow_sidechain:
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    session = record.get("sessionId")
    if not isinstance(session, str) or not session:
        raise UnsupportedTranscript("claude_record_invalid")
    if expected_session is not None and session != expected_session:
        raise UnsupportedTranscript("claude_session_mismatch")
    request_id = record.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        candidate = message.get("id")
        request_id = candidate if isinstance(candidate, str) and candidate else None
    if request_id is None:
        raise UnsupportedTranscript("claude_record_invalid")
    counters = _claude_counters(message["usage"])
    model = message.get("model") if isinstance(message.get("model"), str) else None
    version = record.get("version") if isinstance(record.get("version"), str) else None
    stop_reason = (
        message.get("stop_reason") if isinstance(message.get("stop_reason"), str) else None
    )
    return _ClaudeResponse(
        request_id=request_id,
        counters=counters,
        model=model,
        version=version,
        occurred_at=_occurred_at(record.get("timestamp")),
        stop_reason=stop_reason,
    )


def _claude_counters(usage: dict[str, Any]) -> dict[str, int | None]:
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    raw = {
        "input_tokens": usage.get("input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": thinking,
    }
    result: dict[str, int | None] = {}
    for name in CLAUDE_COUNTERS:
        item = raw[name]
        if item is not None and (type(item) is not int or item < 0):
            raise UnsupportedTranscript("claude_usage_invalid")
        result[name] = item
    return result


def _claude_usage_event(
    store: "Store", source: Mapping[str, object], response: _ClaudeResponse
) -> Envelope:
    session_id, agent_id = _split_subagent(source)
    # One request is one usage event per conversation agent, however the
    # transcript path is spelled.
    name = f"{CLAUDE_READER}|{session_id}|{agent_id or ''}|{response.request_id}"
    availability: dict[str, Availability] = {
        key: "observed" if response.counters[key] is not None else "unavailable"
        for key in CLAUDE_OBSERVED_KEYS
    }
    availability["model"] = "observed" if response.model is not None else "unavailable"
    return Envelope(
        event_id=uuid5(NAMESPACE_URL, name),
        provider="claude",
        project_id=store.project_id,
        session_id=session_id,
        agent_id=agent_id,
        turn_id=None,
        native_event_id=response.request_id,
        kind="usage",
        source="transcript",
        occurred_at=response.occurred_at,
        payload={
            "claude": {
                "reader": CLAUDE_READER,
                "model": response.model,
                "cc_version": response.version,
                "request_id": response.request_id,
                "usage": {"response": dict(response.counters)},
            }
        },
        availability=availability,
    )


def _load_counters(value: object) -> dict[str, int | None] | None:
    if not isinstance(value, str):
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    try:
        return _counters(loaded)
    except UnsupportedTranscript:
        return None


def _update_failure(
    store: "Store",
    source: dict[str, object],
    code: TranscriptFailureCode,
    signature: str,
    *,
    device: int | None = None,
    inode: int | None = None,
    size: int | None = None,
    mtime: int | None = None,
) -> bool:
    changed = source["last_error"] != code or source["error_signature"] != signature
    if not changed:
        return False
    _gap(store, source, code, signature)
    store.update_transcript_source(
        source,
        reader=None,
        device=device,
        inode=inode,
        size=size,
        mtime=mtime,
        offset=0,
        tail=b"",
        counters=_load_counters(source["counters"]),
        last_error=code,
        error_signature=signature,
    )
    return True


def _normalize_source(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsupportedTranscript("transcript_source_invalid")
    source = value.copy()
    provider = source.get("provider")
    session_id = source.get("session_id")
    path_value = source.get("path")
    if (
        provider not in ("codex", "claude")
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(path_value, str)
        or not path_value
        or len(path_value) > 4096
        or not Path(path_value).is_absolute()
    ):
        raise UnsupportedTranscript("transcript_source_invalid")
    for name in ("reader", "device", "inode", "size", "mtime", "counters", "last_error"):
        source.setdefault(name, None)
    source.setdefault("offset", 0)
    source.setdefault("tail", b"")
    source.setdefault("error_signature", None)
    return source


def _source_is_current(source: Mapping[str, object], active_failure_since: datetime | None) -> bool:
    """Keep an unverifiable source failure visible rather than silently hiding it."""
    if active_failure_since is None:
        return True
    last_seen = source.get("last_seen")
    if not isinstance(last_seen, str):
        return True
    try:
        observed_at = datetime.fromisoformat(last_seen)
    except ValueError:
        return True
    if observed_at.tzinfo is None:
        return True
    return observed_at >= active_failure_since


def _process_source(store: "Store", source: dict[str, object]) -> EnrichmentResult:
    path = Path(str(source["path"]))
    try:
        device, inode, size, mtime = _signature(path)
    except OSError:
        changed = _update_failure(store, source, "transcript_unreadable", "unreadable")
        failures = ("transcript_unreadable",) if changed else ()
        return EnrichmentResult(failures=failures, active_failures=("transcript_unreadable",))
    except UnsupportedTranscript as error:
        changed = _update_failure(store, source, error.code, "invalid")
        failures = (error.code,) if changed else ()
        return EnrichmentResult(failures=failures, active_failures=(error.code,))

    signature = f"{device}:{inode}:{size}:{mtime}"
    if (
        source["last_error"]
        and source["error_signature"] == signature
        and not _stopped_by_former_reader(source)
    ):
        code = source["last_error"]
        if isinstance(code, str) and code in FAILURE_CODES:
            return EnrichmentResult(active_failures=(cast(TranscriptFailureCode, code),))
        return EnrichmentResult(active_failures=("transcript_source_invalid",))
    stored_offset = source["offset"]
    offset = stored_offset if type(stored_offset) is int and stored_offset >= 0 else 0
    tail = source["tail"] if isinstance(source["tail"], bytes) else b""
    reader = source["reader"] if isinstance(source["reader"], str) else None
    if source["last_error"] or (
        source["device"] != device
        or source["inode"] != inode
        or size < offset
        or (size == offset and source["mtime"] is not None and source["mtime"] != mtime)
    ):
        offset, tail, reader = 0, b"", None
    oversized = False
    skip_to: int | None = None
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            carried = _unfinished_length(tail)
            raw = _read_window(stream, carried)
            unfinished = _unfinished_length(raw) if b"\n" in raw else carried + len(raw)
            if unfinished > MAX_LINE_BYTES:
                # Too long to hold: skip the line whole instead. While it is still
                # being written, its end is unknown and it is read again next pass.
                oversized = True
                skip_to = _line_end(stream)
    except OSError:
        changed = _update_failure(
            store,
            source,
            "transcript_unreadable",
            signature,
            device=device,
            inode=inode,
            size=size,
            mtime=mtime,
        )
        failures = ("transcript_unreadable",) if changed else ()
        return EnrichmentResult(failures=failures, active_failures=("transcript_unreadable",))
    stalled = not raw
    if stalled and not (source["provider"] == "claude" and tail):
        return EnrichmentResult()
    content = tail + raw
    lines = content.splitlines(keepends=True)
    if lines and not lines[-1].endswith((b"\n", b"\r")):
        tail = lines.pop()
    else:
        tail = b""
    resume = offset + len(raw)
    if oversized:
        # Resume past the skipped line, or at its start until it is complete.
        resume, tail = (skip_to, b"") if skip_to is not None else (resume - len(tail), b"")
    counters = _load_counters(source["counters"])
    if source["provider"] == "claude":
        consumed = _consume_claude(store, source, lines, signature, stalled=stalled)
    else:
        consumed = _consume_codex(store, source, lines, signature, reader, counters)
    failures = list(consumed.failures)
    if skip_to is not None and consumed.error is None:
        _gap(store, source, "transcript_line_oversized", signature)
        failures.append("transcript_line_oversized")
    store.update_transcript_source(
        source,
        reader=consumed.reader,
        device=device,
        inode=inode,
        size=size,
        mtime=mtime,
        offset=resume,
        # A held-open response's bytes precede any trailing partial line.
        tail=consumed.held_tail + tail,
        counters=consumed.counters,
        last_error=consumed.error,
        error_signature=signature if consumed.error else None,
    )
    return EnrichmentResult(
        accepted=consumed.accepted,
        failures=tuple(failures),
        active_failures=tuple(failures),
        inert=consumed.usage_seen > 0 and consumed.accepted == 0 and consumed.error is None,
    )


def _stopped_by_former_reader(source: Mapping[str, object]) -> bool:
    """A source an earlier reader version stopped for good; read it once more.

    Its error signature equals the unchanged file's, so it would never be read
    again. Neither state below is produced by the current reader, so a retry
    that fails again stores a different state and is not repeated:

    - WD-130: the former 1 MiB line limit left ``rollout_line_invalid`` at
      offset 0 with no tail.
    - WD-131: a Codex subagent rollout reported under its parent session was
      rejected as ``session_meta_mismatch``; it is now read as that subagent,
      or fails as ``subagent_meta_mismatch``.
    """
    if source["last_error"] == "rollout_line_invalid":
        return source["offset"] == 0 and not source["tail"]
    return (
        source["last_error"] == "session_meta_mismatch"
        and source["provider"] == "codex"
        and _rollout_agent(source) is not None
    )


def _unfinished_length(data: bytes) -> int:
    """Length of the unfinished line at the end of ``data``."""
    return len(data) - (data.rfind(b"\n") + 1)


def _read_window(stream: BinaryIO, carried: int) -> bytes:
    """Read the next window, extended while it lies inside one unfinished line.

    ``carried`` is that line's length already held in the stored tail. Reading
    stops at the line's end, at EOF, or once the line passes MAX_LINE_BYTES, so
    a long line is read whole in one pass instead of piling up in the tail.
    """
    chunks = [stream.read(READ_BYTES)]
    while len(chunks[-1]) == READ_BYTES and b"\n" not in chunks[-1]:
        carried += READ_BYTES
        if carried > MAX_LINE_BYTES:
            break
        chunks.append(stream.read(READ_BYTES))
    return b"".join(chunks)


def _line_end(stream: BinaryIO) -> int | None:
    """Offset just past the next line break from the stream position, or None at EOF."""
    while chunk := stream.read(READ_BYTES):
        cut = chunk.find(b"\n")
        if cut >= 0:
            return stream.tell() - len(chunk) + cut + 1
    return None


@dataclass
class _Consumed:
    accepted: int
    failures: list[TranscriptFailureCode]
    reader: str | None
    counters: dict[str, int | None] | None
    error: TranscriptFailureCode | None
    usage_seen: int = 0
    # Raw bytes of an in-progress Claude response's blocks, held back so the
    # next read window resumes it instead of losing it or emitting early.
    held_tail: bytes = b""


def _consume_codex(
    store: "Store",
    source: dict[str, object],
    lines: list[bytes],
    signature: str,
    reader: str | None,
    counters: dict[str, int | None] | None,
) -> _Consumed:
    accepted = 0
    usage_seen = 0
    error: TranscriptFailureCode | None = None
    failures: list[TranscriptFailureCode] = []
    stored: set[str] | None = None
    agent_id = _rollout_agent(source)
    try:
        for line in lines:
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UnsupportedTranscript("rollout_line_invalid") from exc
            if reader is None:
                _meta_record(record, str(source["session_id"]), agent_id)
                reader = READER
                continue
            if not isinstance(record, dict):
                raise UnsupportedTranscript("rollout_record_invalid")
            if record.get("type") != "token_usage_record":
                continue
            payload, current = _usage_record(record, str(source["session_id"]))
            if stored is None:
                stored = store.usage_native_ids("codex", str(source["session_id"]), agent_id)
            if payload["response_id"] in stored:
                # Already stored, possibly under an earlier path- or offset-derived
                # event id: a re-read after a reset, or another spelling of this
                # rollout. Only advance the cumulative baseline.
                counters = current
                continue
            usage_seen += 1
            delta, reset = _delta(counters, current)
            if reset:
                _gap(store, source, "usage_counter_reset", signature)
                failures.append("usage_counter_reset")
            if store.put(_usage_event(store, source, record, current, delta)):
                accepted += 1
            stored.add(payload["response_id"])
            counters = current
    except UnsupportedTranscript as exc:
        error = exc.code
        if source["last_error"] != error or source["error_signature"] != signature:
            _gap(store, source, error, signature)
            failures.append(error)
        reader = None
    return _Consumed(accepted, failures, reader, counters, error, usage_seen=usage_seen)


# A held, still-open response is bounded so that it and one unfinished line of
# up to MAX_LINE_BYTES fit the ``tail`` column (storage.TRANSCRIPT_TAIL_BYTES).
_MAX_HELD_TAIL = 512 * 1024


def _consume_claude(
    store: "Store",
    source: dict[str, object],
    lines: list[bytes],
    signature: str,
    *,
    stalled: bool,
) -> _Consumed:
    """Emit one usage event per closed Claude response.

    A response's ``message.usage`` can grow across its content blocks (most
    visibly on subagent responses); the last block, marked by a non-null
    ``stop_reason``, carries the true final value. A request id is treated as
    closed either by that signal or by a later, distinct request id following
    it in this batch -- only the single most-recent (last-seen) request id can
    still be genuinely in progress. That trailing response is held (its raw
    bytes carried into the next read window) unless the source has stopped
    growing (``stalled``) or holding it would exceed the bound above, in which
    case it is emitted with its last-known value rather than held forever; the
    latter also records a ``claude_response_oversized`` gap, since that value
    may undercount.
    """
    parent, agent_id = _split_subagent(source)
    is_subagent = agent_id is not None
    bound_session: str | None = None if is_subagent else parent
    order: list[str] = []
    latest: dict[str, _ClaudeResponse] = {}
    closed: set[str] = set()
    blocks: dict[str, list[bytes]] = {}
    stored: set[str] | None = None
    accepted = 0
    usage_seen = 0
    held_tail = b""
    error: TranscriptFailureCode | None = None
    failures: list[TranscriptFailureCode] = []
    try:
        for line in lines:
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UnsupportedTranscript("claude_line_invalid") from exc
            if _has_usage(record):
                usage_seen += 1
            response = _claude_response(record, bound_session, allow_sidechain=is_subagent)
            if response is None:
                continue
            if bound_session is None:
                bound_session = cast(dict, record)["sessionId"]
            request_id = response.request_id
            if request_id not in latest:
                order.append(request_id)
                blocks[request_id] = []
            latest[request_id] = response
            blocks[request_id].append(line)
            if response.stop_reason is not None:
                closed.add(request_id)
        pending_id = order[-1] if order else None
        for request_id in order:
            early = False
            if request_id == pending_id and request_id not in closed:
                candidate_tail = b"".join(blocks[request_id])
                if not stalled and len(candidate_tail) <= _MAX_HELD_TAIL:
                    held_tail = candidate_tail
                    usage_seen -= len(blocks[request_id])
                    continue
                # Source stopped growing, or the held response is too large to
                # hold: flush its last-known value instead of holding it forever.
                early = not stalled
            if stored is None:
                stored = store.usage_native_ids("claude", parent, agent_id)
            if request_id in stored:
                # Already stored, possibly under an earlier path-derived event id
                # or by a prior first-block-wins reader version: never overwrite
                # or add it, and do not report its lines as unstored usage.
                usage_seen -= len(blocks[request_id])
                continue
            if store.put(_claude_usage_event(store, source, latest[request_id])):
                accepted += 1
            stored.add(request_id)
            if early:
                # Emitted before its final block, so the value may undercount.
                _gap(store, source, "claude_response_oversized", signature)
                failures.append("claude_response_oversized")
    except UnsupportedTranscript as exc:
        error = exc.code
        if source["last_error"] != error or source["error_signature"] != signature:
            _gap(store, source, error, signature)
            failures.append(error)
        held_tail = b""
    return _Consumed(
        accepted, failures, CLAUDE_READER, None, error, usage_seen=usage_seen, held_tail=held_tail
    )


def _unexpected_source_failure(store: "Store", source: dict[str, object]) -> EnrichmentResult:
    try:
        device, inode, size, mtime = _signature(Path(str(source["path"])))
        signature = f"{device}:{inode}:{size}:{mtime}"
    except (OSError, UnsupportedTranscript, ValueError):
        device, inode, size, mtime = None, None, None, None
        signature = "invalid"
    changed = _update_failure(
        store,
        source,
        "transcript_source_invalid",
        signature,
        device=device,
        inode=inode,
        size=size,
        mtime=mtime,
    )
    failures = ("transcript_source_invalid",) if changed else ()
    return EnrichmentResult(
        failures=failures,
        active_failures=("transcript_source_invalid",),
    )


def enrich(store: "Store", *, active_failure_since: datetime | None = None) -> EnrichmentResult:
    """Read bounded usage from known Codex and Claude transcript paths, daemon only."""
    try:
        sources = store.transcript_sources()
    except StorageError:
        raise
    except (OSError, KeyError, TypeError, ValueError):
        return EnrichmentResult(
            failures=("transcript_sources_unavailable",),
            active_failures=("transcript_sources_unavailable",),
        )

    accepted = 0
    inert = False
    failures: list[TranscriptFailureCode] = []
    active_failures: list[TranscriptFailureCode] = []
    for value in sources:
        try:
            source = _normalize_source(value)
        except UnsupportedTranscript as error:
            failures.append(error.code)
            active_failures.append(error.code)
            continue
        try:
            result = _process_source(store, source)
        except RejectedEvent:
            # A deterministic transcript-derived event can conflict with an
            # older receipt after a provider format change.  This affects only
            # this enrichment source; hook ingestion and other sources remain
            # available. Do not surface its message or transcript details.
            result = EnrichmentResult(
                failures=("transcript_source_invalid",),
                active_failures=("transcript_source_invalid",),
            )
        except StorageError:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            result = _unexpected_source_failure(store, source)
        accepted += result.accepted
        inert = inert or result.inert
        for code in result.failures:
            if code not in failures:
                failures.append(code)
        if _source_is_current(source, active_failure_since):
            for code in result.active_failures:
                if code not in active_failures:
                    active_failures.append(code)
    return EnrichmentResult(
        accepted=accepted,
        inert=inert and accepted == 0,
        failures=tuple(failures),
        active_failures=tuple(active_failures),
    )
