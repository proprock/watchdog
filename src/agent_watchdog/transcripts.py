"""Version-gated, daemon-only transcript enrichment for Codex and Claude.

Codex uses the ``codex-rollout-v1`` reader (cumulative ``thread_token_usage``
deltas). Claude uses the ``claude-transcript-v1`` reader: each assistant response
carries its own ``message.usage`` (not cumulative), repeated on every content
block of the response, so one ``usage`` event is emitted per distinct
``requestId``. Both readers share the file-identity, offset, partial-tail,
rotation and resume machinery below.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5

from agent_watchdog.events import Availability, Envelope
from agent_watchdog.storage import RejectedEvent, StorageError

if TYPE_CHECKING:
    from agent_watchdog.storage import Store


READER = "codex-rollout-v1"
CLAUDE_READER = "claude-transcript-v1"
READ_BYTES = 1024**2
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


def _meta_record(record: object, session_id: str) -> None:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise UnsupportedTranscript("session_meta_missing")
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("id") != session_id:
        raise UnsupportedTranscript("session_meta_mismatch")
    if not isinstance(payload.get("cli_version"), str):
        raise UnsupportedTranscript("session_meta_version_missing")


def _identifier(
    source: dict[str, object], identity: tuple[int, int], offset: int, line: bytes
) -> str:
    digest = hashlib.sha256(line).hexdigest()
    name = f"{READER}|{source['path']}|{identity[0]}:{identity[1]}|{offset}|{digest}"
    return str(uuid5(NAMESPACE_URL, name))


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
    identity: tuple[int, int],
    offset: int,
    line: bytes,
) -> Envelope:
    payload = record["payload"]
    occurred_at = _occurred_at(record.get("timestamp"))
    event_id = uuid5(NAMESPACE_URL, _identifier(source, identity, offset, line))
    return Envelope(
        event_id=event_id,
        provider="codex",
        project_id=store.project_id,
        session_id=str(source["session_id"]),
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
    return _ClaudeResponse(
        request_id=request_id,
        counters=counters,
        model=model,
        version=version,
        occurred_at=_occurred_at(record.get("timestamp")),
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
    name = f"{CLAUDE_READER}|{source['path']}|{session_id}|{response.request_id}"
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
    if source["last_error"] and source["error_signature"] == signature:
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
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(READ_BYTES)
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
    if not raw:
        return EnrichmentResult()
    content = tail + raw
    lines = content.splitlines(keepends=True)
    if lines and not lines[-1].endswith((b"\n", b"\r")):
        tail = lines.pop()
    else:
        tail = b""
    if len(tail) > 1024**2:
        changed = _update_failure(
            store,
            source,
            "rollout_line_invalid",
            signature,
            device=device,
            inode=inode,
            size=size,
            mtime=mtime,
        )
        failures = ("rollout_line_invalid",) if changed else ()
        return EnrichmentResult(failures=failures, active_failures=("rollout_line_invalid",))
    line_offset = offset - len(source["tail"] if isinstance(source["tail"], bytes) else b"")
    counters = _load_counters(source["counters"])
    if source["provider"] == "claude":
        consumed = _consume_claude(store, source, lines, signature)
    else:
        consumed = _consume_codex(
            store, source, lines, line_offset, (device, inode), signature, reader, counters
        )
    store.update_transcript_source(
        source,
        reader=consumed.reader,
        device=device,
        inode=inode,
        size=size,
        mtime=mtime,
        offset=offset + len(raw),
        tail=tail,
        counters=consumed.counters,
        last_error=consumed.error,
        error_signature=signature if consumed.error else None,
    )
    return EnrichmentResult(
        accepted=consumed.accepted,
        failures=tuple(consumed.failures),
        active_failures=tuple(consumed.failures),
        inert=consumed.usage_seen > 0 and consumed.accepted == 0 and consumed.error is None,
    )


@dataclass
class _Consumed:
    accepted: int
    failures: list[TranscriptFailureCode]
    reader: str | None
    counters: dict[str, int | None] | None
    error: TranscriptFailureCode | None
    usage_seen: int = 0


def _consume_codex(
    store: "Store",
    source: dict[str, object],
    lines: list[bytes],
    line_offset: int,
    identity: tuple[int, int],
    signature: str,
    reader: str | None,
    counters: dict[str, int | None] | None,
) -> _Consumed:
    accepted = 0
    usage_seen = 0
    error: TranscriptFailureCode | None = None
    failures: list[TranscriptFailureCode] = []
    try:
        for line in lines:
            current_offset = line_offset
            line_offset += len(line)
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UnsupportedTranscript("rollout_line_invalid") from exc
            if reader is None:
                _meta_record(record, str(source["session_id"]))
                reader = READER
                continue
            if not isinstance(record, dict):
                raise UnsupportedTranscript("rollout_record_invalid")
            if record.get("type") != "token_usage_record":
                continue
            usage_seen += 1
            payload, current = _usage_record(record, str(source["session_id"]))
            delta, reset = _delta(counters, current)
            if reset:
                _gap(store, source, "usage_counter_reset", signature)
                failures.append("usage_counter_reset")
            store.put(
                _usage_event(store, source, record, current, delta, identity, current_offset, line)
            )
            counters = current
            accepted += 1
    except UnsupportedTranscript as exc:
        error = exc.code
        if source["last_error"] != error or source["error_signature"] != signature:
            _gap(store, source, error, signature)
            failures.append(error)
        reader = None
    return _Consumed(accepted, failures, reader, counters, error, usage_seen=usage_seen)


def _consume_claude(
    store: "Store", source: dict[str, object], lines: list[bytes], signature: str
) -> _Consumed:
    parent, agent_id = _split_subagent(source)
    is_subagent = agent_id is not None
    bound_session: str | None = None if is_subagent else parent
    seen: set[str] = set()
    accepted = 0
    usage_seen = 0
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
            if response.request_id in seen:
                continue
            seen.add(response.request_id)
            # A response is repeated on every content block; the deterministic
            # event id makes a repeat that lands in a later read window an
            # idempotent no-op rather than a second row.
            if store.put(_claude_usage_event(store, source, response)):
                accepted += 1
    except UnsupportedTranscript as exc:
        error = exc.code
        if source["last_error"] != error or source["error_signature"] != signature:
            _gap(store, source, error, signature)
            failures.append(error)
    return _Consumed(accepted, failures, CLAUDE_READER, None, error, usage_seen=usage_seen)


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
