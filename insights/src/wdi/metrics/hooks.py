"""Metrics from imported hook observations and transcript usage."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any


def session_hook_metrics(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Summarize observed waits, tool durations, and delivered controls."""
    rows = conn.execute(
        """SELECT f.*, e.envelope FROM hook_facts f JOIN hook_events e
           ON e.machine_id=f.machine_id AND e.project_id=f.project_id
              AND e.event_rowid=f.event_rowid
           JOIN sessions s ON s.session_id=f.session_id AND s.machine_id=f.machine_id
              AND s.agent=f.provider
           WHERE f.session_id=? ORDER BY f.machine_id, f.project_id, f.event_rowid""",
        (session_id,),
    )
    pending: dict[tuple[Any, ...], int | None] = {}
    waits = completed_ms = incomplete = 0
    durations: dict[str, int | None] = {}
    controls: Counter[tuple[str, str]] = Counter()
    for row in rows:
        kind = row["kind"]
        identity = (
            row["machine_id"],
            row["project_id"],
            row["provider"],
            row["agent_id"],
            row["turn_id"],
        )
        if kind == "waiting" and row["notification_type"] == "permission_prompt":
            if identity not in pending:
                waits += 1
                pending[identity] = row["received_at_us"]
        elif (
            kind in ("tool.start", "tool.finish", "turn.start", "turn.end") and identity in pending
        ):
            start = pending.pop(identity)
            end = row["received_at_us"]
            if start is None or end is None or end < start:
                incomplete += 1
            else:
                completed_ms += (end - start) // 1000
        if kind == "tool.finish" and row["tool_name"]:
            name = row["tool_name"]
            value = row["tool_duration_ms"]
            if name not in durations:
                durations[name] = value
            elif durations[name] is None or value is None:
                durations[name] = None
            else:
                durations[name] += value
        if kind == "control":
            try:
                envelope = json.loads(row["envelope"])
                payload = envelope.get("payload", {}).get(row["provider"], {})
                action, rule = payload.get("action"), payload.get("rule")
                if (
                    (payload.get("response_sent_at") or payload.get("delivered_at"))
                    and isinstance(action, str)
                    and isinstance(rule, str)
                ):
                    controls[(action, rule)] += 1
            except TypeError, ValueError, AttributeError:
                continue
    incomplete += len(pending)
    return {
        "permission_wait_ms": completed_ms if waits and not incomplete else None,
        "permission_waits": waits,
        "incomplete_permission_waits": incomplete,
        "tool_durations_ms": durations,
        "control_actions": [
            {"action": action, "rule": rule, "count": count}
            for (action, rule), count in sorted(controls.items())
        ],
    }


def reconcile_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compare transcript and hook usage without changing provider accounting."""
    dimensions = {
        "input_tokens": "input_tokens",
        "cached_input_tokens": "cache_read_tokens",
        "cache_write_input_tokens": "cache_creation_tokens",
        "output_tokens": "output_tokens",
    }
    sessions = conn.execute(
        """SELECT DISTINCT s.session_id, s.machine_id, s.agent
           FROM sessions s JOIN hook_facts f
             ON f.session_id=s.session_id AND f.machine_id=s.machine_id AND f.provider=s.agent
           WHERE f.kind='usage'
             AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.session_id
                         AND m.machine_id=s.machine_id)"""
    ).fetchall()
    candidates = {tuple(session) for session in sessions}
    # A missing value in any contributing usage row makes that dimension unknown.
    totals: dict[tuple[Any, ...], int | None] = {}

    def add(key: tuple[Any, ...], value: int | None) -> None:
        if key not in totals:
            totals[key] = value
        else:
            previous = totals[key]
            totals[key] = None if previous is None or value is None else previous + value

    messages = conn.execute(
        """SELECT m.*, s.agent, s.parent_session_id FROM messages m
           JOIN sessions s ON s.session_id=m.session_id AND s.machine_id=m.machine_id
           WHERE ((s.agent='claude' AND m.role='assistant') OR
                  (s.agent='codex' AND m.role='event')) AND
             (m.input_tokens IS NOT NULL OR m.output_tokens IS NOT NULL OR
              m.cache_read_tokens IS NOT NULL OR m.cache_creation_tokens IS NOT NULL)"""
    )
    for row in messages:
        own = (row["session_id"], row["machine_id"], row["agent"])
        scope = "main" if row["stream_id"] == "main" else "subagent"
        parent = (row["parent_session_id"], row["machine_id"], row["agent"])
        targets = []
        if own in candidates:
            targets.append((own, scope))
        if parent in candidates:
            targets.append((parent, "subagent"))
        for target, target_scope in targets:
            for metric, column in dimensions.items():
                add(("wdi", *target, target_scope, metric), row[column])

    seen: set[tuple[Any, ...]] = set()
    facts = conn.execute(
        """SELECT * FROM hook_facts WHERE kind='usage'
           ORDER BY machine_id, project_id, event_rowid"""
    )
    for row in facts:
        target = (row["session_id"], row["machine_id"], row["provider"])
        if target not in candidates:
            continue
        if row["native_event_id"] is not None:
            identity = (
                row["machine_id"],
                row["project_id"],
                row["provider"],
                row["session_id"],
                row["agent_id"],
                row["native_event_id"],
            )
            if identity in seen:
                continue
            seen.add(identity)
        scope = "main" if row["agent_id"] is None else "subagent"
        for metric in dimensions:
            add(("watchdog", *target, scope, metric), row[metric])

    results = []
    for session in sessions:
        session_id, machine_id, provider = session
        for scope in ("main", "subagent"):
            for metric in dimensions:
                wdi = totals.get(("wdi", session_id, machine_id, provider, scope, metric))
                watchdog = totals.get(("watchdog", session_id, machine_id, provider, scope, metric))
                differs = (
                    None
                    if wdi is None or watchdog is None
                    else abs(wdi - watchdog) * 100 > abs(watchdog)
                )
                results.append(
                    {
                        "session_id": session_id,
                        "machine_id": machine_id,
                        "provider": provider,
                        "scope": scope,
                        "metric": metric,
                        "wdi": wdi,
                        "watchdog": watchdog,
                        "differs": differs,
                    }
                )
    return results
