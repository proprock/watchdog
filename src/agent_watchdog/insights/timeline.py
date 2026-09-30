"""Per-request context occupancy and what filled it, shared by `context` and `tokens`.

Measured: every usage row is one model request, and its input side is the context the
model read. Claude reports disjoint counters (input + cache read + cache write); Codex
reports `input_tokens` with cached tokens as a subset (seen on live rollout data).
Inferred: the growth between two consecutive requests of one agent, minus the earlier
answer, is what was appended in between (tool results, prompts, reminders); it is
matched to the tool calls that finished in that interval by time.
"""

import bisect
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_watchdog.analysis import captured_content
from agent_watchdog.events import Envelope
from agent_watchdog.storage import persisted_envelope

Key = tuple[str, str | None, str | None]
# A request whose context is below this share of the previous one was reset:
# a compaction, a clear, or a resumed conversation.
RESET_SHARE = 0.7
# A request this much larger than its predecessor, followed by a return to the
# earlier level, did not continue the conversation. Seen live: one response
# whose usage sums two API iterations (cache reads doubled), and side requests.
SPIKE_RATIO = 1.5
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class Request:
    event_id: str
    at_us: int
    occupancy: int
    output: int | None
    cache_write: int | None
    model: str | None


@dataclass
class Step:
    """The interval before request ``index`` of one agent."""

    key: Key
    index: int
    before: Request
    after: Request
    finishes: list[Envelope] = field(default_factory=list)

    @property
    def reset(self) -> bool:
        return self.after.occupancy < self.before.occupancy * RESET_SHARE

    @property
    def appended(self) -> int:
        if self.reset:
            return 0
        return max(0, self.after.occupancy - self.before.occupancy - (self.before.output or 0))


def us(value: datetime | None) -> int | None:
    return round(value.timestamp() * 1_000_000) if value is not None else None


def occupancy(provider: str, row: dict[str, Any]) -> int | None:
    input_tokens = row["input_tokens"]
    if input_tokens is None:
        return None
    if provider == "claude":
        cached, written = row["cached_input_tokens"], row["cache_write_input_tokens"]
        if cached is None or written is None:
            return None
        return input_tokens + cached + written
    return input_tokens


def requests(
    db: sqlite3.Connection,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[dict[Key, list[Request]], dict[str, int]]:
    """Load each agent's requests in time order, dropping re-emitted responses."""
    where, params = "kind = 'usage'", []
    for clause, value in (
        ("provider = ?", provider),
        ("session_id = ?", session_id),
        ("received_at_us >= ?", us(since)),
        ("received_at_us < ?", us(until)),
    ):
        if value is not None:
            where += f" AND {clause}"
            params.append(value)
    cursor = db.execute(
        "SELECT event_id, provider, session_id, agent_id, native_event_id, "
        "COALESCE(occurred_at_us, received_at_us) AS at_us, model, input_tokens, "
        "cached_input_tokens, cache_write_input_tokens, output_tokens "
        f"FROM event_facts WHERE {where} ORDER BY at_us, event_id",
        params,
    )
    names = [column[0] for column in cursor.description]
    series: dict[Key, list[Request]] = {}
    seen: set[tuple] = set()
    counts = {
        "usage_rows": 0,
        "duplicate_usage_rows": 0,
        "unknown_occupancy_rows": 0,
        "synthetic_rows": 0,
        "spike_rows_excluded": 0,
    }
    for values in cursor.fetchall():
        row = dict(zip(names, values, strict=True))
        counts["usage_rows"] += 1
        key: Key = (row["provider"], row["session_id"], row["agent_id"])
        if row["native_event_id"] is not None:
            identity = (*key, row["native_event_id"])
            if identity in seen:
                counts["duplicate_usage_rows"] += 1
                continue
            seen.add(identity)
        if row["model"] == SYNTHETIC_MODEL:
            # Claude Code writes zero-usage placeholder messages under this model name.
            counts["synthetic_rows"] += 1
            continue
        size = occupancy(row["provider"], row)
        if size is None:
            counts["unknown_occupancy_rows"] += 1
            continue
        series.setdefault(key, []).append(
            Request(
                event_id=row["event_id"],
                at_us=row["at_us"],
                occupancy=size,
                output=row["output_tokens"],
                cache_write=row["cache_write_input_tokens"],
                model=row["model"],
            )
        )
    for key, sequence in series.items():
        kept = _without_spikes(sequence)
        counts["spike_rows_excluded"] += len(sequence) - len(kept)
        series[key] = kept
    return series, counts


def _without_spikes(sequence: list[Request]) -> list[Request]:
    kept: list[Request] = []
    for index, request in enumerate(sequence):
        following = sequence[index + 1] if index + 1 < len(sequence) else None
        if (
            kept
            and following is not None
            and request.occupancy > kept[-1].occupancy * SPIKE_RATIO
            and following.occupancy < request.occupancy * RESET_SHARE
            # A return to the earlier level, not a compaction below it.
            and following.occupancy >= kept[-1].occupancy * RESET_SHARE
        ):
            continue
        kept.append(request)
    return kept


def steps(
    series: dict[Key, list[Request]], finishes: list[Envelope]
) -> tuple[list[Step], list[Envelope]]:
    """Pair consecutive requests and attach the tool calls that finished between them."""
    result: list[Step] = []
    by_key: dict[Key, list[Step]] = {}
    for key, sequence in series.items():
        for index in range(1, len(sequence)):
            step = Step(key, index, sequence[index - 1], sequence[index])
            result.append(step)
            by_key.setdefault(key, []).append(step)
    bounds = {key: [step.after.at_us for step in values] for key, values in by_key.items()}
    unattributed: list[Envelope] = []
    for event in finishes:
        key: Key = (event.provider, event.session_id, event.agent_id)
        candidates = by_key.get(key, [])
        at = round(event.received_at.timestamp() * 1_000_000)
        position = bisect.bisect_left(bounds.get(key, []), at)
        if position < len(candidates) and candidates[position].before.at_us < at:
            candidates[position].finishes.append(event)
        else:
            unattributed.append(event)
    return result, unattributed


def events(
    db: sqlite3.Connection,
    kinds: tuple[str, ...],
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Envelope]:
    """Load envelopes of the given kinds in received order, filtered like the other reads."""
    where = f"kind IN ({', '.join('?' for _ in kinds)})"
    params: list[object] = list(kinds)
    if provider is not None:
        where += " AND json_extract(envelope, '$.provider') = ?"
        params.append(provider)
    if session_id is not None:
        where += " AND session_id = ?"
        params.append(session_id)
    result = []
    for (document,) in db.execute(
        f"SELECT envelope FROM events WHERE {where} ORDER BY received_at", params
    ):
        event = persisted_envelope(document)
        if since is not None and event.received_at < since:
            continue
        if until is not None and event.received_at >= until:
            continue
        result.append(event)
    return result


def namespace(event: Envelope) -> dict[str, Any]:
    value = event.payload.get(event.provider)
    return value if isinstance(value, dict) else {}


def metadata(event: Envelope) -> dict[str, Any]:
    value = namespace(event).get("metadata")
    return value if isinstance(value, dict) else {}


def content(event: Envelope, field: str) -> Any:
    """Return one retained content field, or None when content was not captured."""
    value = namespace(event).get("content")
    return value.get(field) if isinstance(value, dict) else None


def duration_ms(event: Envelope) -> int | None:
    value = metadata(event).get("duration_ms")
    return value if type(value) is int and value >= 0 else None


def tool_name(event: Envelope) -> str:
    payload = event.payload.get(event.provider)
    name = payload.get("tool_name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) else "(unknown)"


def tool_content(event: Envelope) -> tuple[Any, Any]:
    """Return captured (tool_input, tool_response); None where not retained."""
    payload = event.payload.get(event.provider)
    if not isinstance(payload, dict):
        return None, None
    content = captured_content(payload)
    return content.get("tool_input"), content.get("tool_response")
