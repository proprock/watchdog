"""Version-gated, daemon-only Codex rollout transcript enrichment."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from agent_watchdog.events import Availability, Envelope

if TYPE_CHECKING:
    from agent_watchdog.storage import Store


READER = "codex-rollout-v1"
READ_BYTES = 1024**2
COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class TranscriptSource:
    provider: str
    session_id: str
    path: str


class UnsupportedTranscript(ValueError):
    pass


def source_from_hook(event: Envelope) -> TranscriptSource | None:
    """Return a durable reader source only for a valid Codex hook reference."""
    if event.provider != "codex" or event.source != "hook" or not event.session_id:
        return None
    payload = event.payload.get("codex")
    if not isinstance(payload, dict):
        return None
    value = payload.get("transcript_path")
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return TranscriptSource("codex", event.session_id, str(path))


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
    event_id = uuid5(
        NAMESPACE_URL,
        f"watchdog|{READER}|gap|{store.project_id}|{source['session_id']}|{source['path']}|{reason}|{signature}",
    )
    event = Envelope(
        event_id=event_id,
        provider="codex",
        project_id=store.project_id,
        session_id=str(source["session_id"]),
        native_event_id=f"transcript-gap:{event_id.hex}",
        kind="observation.gap",
        source="transcript",
        payload={"codex": {"reader": READER, "reason": reason}},
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
    timestamp = record.get("timestamp")
    occurred_at = None
    if isinstance(timestamp, str):
        try:
            candidate = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            occurred_at = candidate if candidate.tzinfo is not None else None
        except ValueError:
            pass
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


def _process_source(store: "Store", source: dict[str, object]) -> int:
    path = Path(str(source["path"]))
    try:
        device, inode, size, mtime = _signature(path)
    except OSError:
        reason, signature = "transcript_unreadable", "unreadable"
        if source["last_error"] != reason or source["error_signature"] != signature:
            _gap(store, source, reason, signature)
        store.update_transcript_source(
            source,
            reader=None,
            device=None,
            inode=None,
            size=None,
            mtime=None,
            offset=0,
            tail=b"",
            counters=_load_counters(source["counters"]),
            last_error=reason,
            error_signature=signature,
        )
        return 0
    except UnsupportedTranscript as error:
        reason, signature = str(error), "invalid"
        if source["last_error"] != reason or source["error_signature"] != signature:
            _gap(store, source, reason, signature)
        store.update_transcript_source(
            source,
            reader=None,
            device=None,
            inode=None,
            size=None,
            mtime=None,
            offset=0,
            tail=b"",
            counters=_load_counters(source["counters"]),
            last_error=reason,
            error_signature=signature,
        )
        return 0

    signature = f"{device}:{inode}:{size}:{mtime}"
    if source["last_error"] and source["error_signature"] == signature:
        return 0
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
        return 0
    if not raw:
        return 0
    content = tail + raw
    lines = content.splitlines(keepends=True)
    if lines and not lines[-1].endswith((b"\n", b"\r")):
        tail = lines.pop()
    else:
        tail = b""
    line_offset = offset - len(source["tail"] if isinstance(source["tail"], bytes) else b"")
    counters = _load_counters(source["counters"])
    accepted = 0
    error: str | None = None
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
            payload, current = _usage_record(record, str(source["session_id"]))
            delta, reset = _delta(counters, current)
            if reset:
                _gap(store, source, "usage_counter_reset", signature)
            store.put(
                _usage_event(
                    store,
                    source,
                    record,
                    current,
                    delta,
                    (device, inode),
                    current_offset,
                    line,
                )
            )
            counters = current
            accepted += 1
    except UnsupportedTranscript as exc:
        error = str(exc)
        if source["last_error"] != error or source["error_signature"] != signature:
            _gap(store, source, error, signature)
        reader = None
    store.update_transcript_source(
        source,
        reader=reader,
        device=device,
        inode=inode,
        size=size,
        mtime=mtime,
        offset=offset + len(raw),
        tail=tail,
        counters=counters,
        last_error=error,
        error_signature=signature if error else None,
    )
    return accepted


def enrich(store: "Store") -> int:
    """Read bounded deltas from known Codex transcript paths in the daemon only."""
    return sum(_process_source(store, source) for source in store.transcript_sources())
