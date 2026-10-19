"""WD-165 temporal configuration projection; persisted envelopes stay authoritative."""

import json
import sqlite3
from typing import Any

from agent_watchdog import facts

VERSION = "wd-165.v1"
_CONTEXT = (
    "json_extract(e.envelope, '$.payload.codex.metadata.record_type') = 'turn_context' "
    "AND json_extract(e.envelope, '$.payload.codex.metadata.attribution_projection') = 'wd-165.v1'"
)


def is_context(envelope: dict[str, Any]) -> bool:
    namespace = envelope.get("payload", {}).get("codex", {})
    metadata = namespace.get("metadata", {}) if isinstance(namespace, dict) else {}
    return (
        envelope.get("provider") == "codex"
        and envelope.get("source") == "transcript"
        and isinstance(metadata, dict)
        and metadata.get("record_type") == "turn_context"
        and metadata.get("attribution_projection") == VERSION
    )


def resolve(db: sqlite3.Connection, envelope: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve the child's own configuration at response time, never at ingest time.

    A turn context is a complete snapshot: a missing field cannot borrow an older
    turn's value. Requested spawn overrides never enter this projection.
    """
    native_at = envelope.get("occurred_at")
    if envelope.get("source") == "transcript" and native_at is None:
        return None, None
    at = facts.epoch_us(native_at or envelope.get("received_at"))
    session = envelope.get("session_id")
    if at is None or not isinstance(session, str):
        return None, None
    turn, _ = facts.turn_of(envelope)
    cursor = db.execute(
        "SELECT f.model, f.reasoning_effort, "
        "COALESCE(f.occurred_at_us,f.received_at_us), f.source FROM event_facts f "
        "JOIN events e ON e.event_id=f.event_id "
        "WHERE f.provider='codex' AND f.session_id=? AND f.agent_id IS ? "
        "AND f.kind != 'usage' AND COALESCE(f.occurred_at_us,f.received_at_us) <= ? "
        "AND (? IS NULL OR f.turn_id IS ?) "
        f"AND ((f.source='transcript' AND ({_CONTEXT}) AND f.occurred_at_us IS NOT NULL) "
        "OR (f.source='hook' AND (f.model_attribution='observed' "
        "OR f.reasoning_effort_attribution='observed'))) "
        "ORDER BY COALESCE(f.occurred_at_us,f.received_at_us) DESC, "
        "(f.source='transcript') DESC, f.event_id DESC",
        (session, envelope.get("agent_id"), at, turn, turn),
    )
    first = cursor.fetchone()
    if first is None:
        return None, None
    model, effort = first[:2]
    for row in cursor:
        if row[2:] != first[2:]:
            break
        if row[0] != first[0]:
            model = None
        if row[1] != first[1]:
            effort = None
    return model, effort


def refresh(
    db: sqlite3.Connection,
    context: dict[str, Any] | None = None,
    *,
    identity: tuple[str, str | None] | None = None,
) -> None:
    """Repair only derived Codex usage fields when historical context arrives."""
    where, params = "f.provider='codex' AND f.kind='usage'", []
    if identity is not None:
        where += " AND f.session_id=? AND f.agent_id IS ?"
        params.extend(identity)
    if context is not None:
        at = facts.epoch_us(context.get("occurred_at"))
        if at is None:
            return
        where += (
            " AND f.session_id=? AND f.agent_id IS ? "
            "AND COALESCE(f.occurred_at_us,f.received_at_us)>=? "
            "AND (f.turn_id IS NULL OR f.turn_id IS ?)"
        )
        turn, _ = facts.turn_of(context)
        params.extend((context.get("session_id"), context.get("agent_id"), at, turn))
    rows = db.execute(
        f"SELECT f.event_id,e.envelope FROM event_facts f "
        f"JOIN events e ON e.event_id=f.event_id WHERE {where}",
        params,
    ).fetchall()
    for event_id, document in rows:
        envelope = json.loads(document)
        projected = facts.project_event(
            event_id, "usage", envelope["received_at"], envelope, inherited=resolve(db, envelope)
        )
        db.execute(
            "UPDATE event_facts SET model=?,model_attribution=?,reasoning_effort=?,"
            "reasoning_effort_attribution=? WHERE event_id=?",
            (
                projected["model"],
                projected["model_attribution"],
                projected["reasoning_effort"],
                projected["reasoning_effort_attribution"],
                event_id,
            ),
        )
