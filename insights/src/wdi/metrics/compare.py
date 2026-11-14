"""Compare observed session metrics without filling observation gaps with zero."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime
from statistics import mean, median
from typing import Any

from agent_watchdog import readapi
from wdi.metrics.environment import _environment, _outcome
from wdi.metrics.hooks import session_hook_metrics

_KEYS = {"project", "provider", "model", "since", "until", "label", "session_ids"}
_METRICS = (
    "user_turns",
    "wall_time_ms",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "estimated_cost",
    "peak_context_tokens",
    "compactions",
    "tool_calls",
    "tool_errors",
    "permission_wait_ms",
    "subagent_runs",
    "files_changed",
    "insertions",
    "deletions",
    "head_moved",
    "outcome_label",
)


def parse_filter(value: str) -> dict:
    try:
        data = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Filter must be a JSON object.") from exc
    if not isinstance(data, dict) or set(data) - _KEYS:
        raise ValueError("Filter must contain only supported keys.")
    for key, item in data.items():
        if key == "session_ids":
            if not isinstance(item, list) or not all(isinstance(x, str) and x for x in item):
                raise ValueError("session_ids must be a list of nonempty strings.")
        elif not isinstance(item, str) or not item:
            raise ValueError(f"{key} must be a nonempty string.")
    if data.get("provider") not in (None, "claude", "codex"):
        raise ValueError("provider must be claude or codex.")
    for key in ("since", "until"):
        if key in data:
            try:
                if date.fromisoformat(data[key]).isoformat() != data[key]:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{key} must be an ISO date (YYYY-MM-DD).") from exc
    if data.get("since", "") > data.get("until", "9999"):
        raise ValueError("since must not be after until.")
    return data


def summarize(values: list[float | int | None]) -> dict[str, Any]:
    known = sorted(value for value in values if value is not None)
    return {
        "count": len(known),
        "unknown": len(values) - len(known),
        "median": median(known) if known else None,
        "mean": mean(known) if known else None,
        "p90": known[math.ceil(len(known) * 0.9) - 1] if known else None,
    }


def _counter(row: sqlite3.Row, usage: sqlite3.Row, column: str, coverage: str) -> int | None:
    return row[column] if usage["n"] and usage[coverage] == usage["n"] else None


def _labels(settings: Any, sessions: list[sqlite3.Row]) -> dict[tuple[str, str, str, str], str]:
    result: dict[tuple[str, str, str, str], str] = {}
    for directory in settings.watchdog.data_dirs:
        for info in readapi.stores(directory):
            if not (info.path / "events.sqlite3").is_file():
                continue
            if info.machine_id is None or info.project_id is None:
                raise ValueError(f"Watchdog store {info.path} has no machine or project identity")
            try:
                with readapi.open_events(info) as db:
                    for session in sessions:
                        if session["machine_id"] != info.machine_id:
                            continue
                        provider, session_id = session["agent"], session["session_id"]
                        outcome = readapi.session_label(db, provider, session_id)["task_outcome"]
                        if outcome and outcome != "unknown":
                            result[(info.machine_id, info.project_id, provider, session_id)] = (
                                outcome
                            )
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ValueError(f"Cannot read Watchdog labels from {info.path}: {exc}") from exc
    return result


def _records(conn: sqlite3.Connection, pricing: Any, settings: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.*, p.canonical_name AS project FROM sessions s "
        "LEFT JOIN projects p ON p.project_id=s.project_id"
    ).fetchall()
    labels = _labels(settings, rows)
    result = []
    for row in rows:
        sid = row["session_id"]
        env = _environment(conn, sid)
        outcome = _outcome(conn, env)
        project_ids = {env["project_id"]} if env else set()
        project_ids.update(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT project_id FROM hook_facts "
                "WHERE session_id=? AND machine_id=? AND provider=?",
                (sid, row["machine_id"], row["agent"]),
            )
        )
        found = {
            labels[(row["machine_id"], p, row["agent"], sid)]
            for p in project_ids
            if (row["machine_id"], p, row["agent"], sid) in labels
        }
        label = next(iter(found)) if len(found) == 1 else None
        messages = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? "
            "AND machine_id=? AND stream_id='main'",
            (sid, row["machine_id"]),
        ).fetchone()[0]
        usage = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(input_tokens) AS i, COUNT(cache_read_tokens) AS r, "
            "COUNT(cache_creation_tokens) AS w, COUNT(output_tokens) AS o, "
            "COUNT(model) AS attributed, COUNT(DISTINCT model) AS models, "
            "MIN(model) AS model, COUNT(ts) AS dated, MIN(substr(ts,1,10)) AS first_day, "
            "MAX(substr(ts,1,10)) AS last_day FROM messages "
            "WHERE session_id=? AND machine_id=? AND stream_id='main' AND "
            "(input_tokens IS NOT NULL OR cache_read_tokens IS NOT NULL OR "
            "cache_creation_tokens IS NOT NULL OR output_tokens IS NOT NULL)",
            (sid, row["machine_id"]),
        ).fetchone()

        inputs = _counter(row, usage, "input_tokens", "i")
        cached = _counter(row, usage, "cache_read_tokens", "r")
        write = _counter(row, usage, "cache_creation_tokens", "w")
        output = _counter(row, usage, "output_tokens", "o")
        cost = None
        complete_usage = all(v is not None for v in (inputs, cached, output)) and (
            write is not None or row["agent"] == "codex"
        )
        attributable = (
            usage["n"]
            and usage["attributed"] == usage["n"]
            and usage["dated"] == usage["n"]
            and usage["models"] == 1
            and usage["model"] == row["model"]
            and usage["first_day"] == usage["last_day"]
            and row["started_at"]
            and usage["first_day"] == row["started_at"][:10]
        )
        if complete_usage and attributable:
            try:
                at = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                cost = pricing.cost(
                    row["model"],
                    input_tokens=inputs,
                    cache_read_tokens=cached,
                    cache_creation_tokens=write if write is not None else 0,
                    output_tokens=output,
                    at=at,
                    provider=row["agent"],
                    input_is_net=row["agent"] != "codex",
                )
            except ValueError:
                pass
        calls, errors, unknown_errors = conn.execute(
            "SELECT COUNT(*), SUM(is_error), "
            "SUM(CASE WHEN is_error=0 AND exit_code IS NULL AND "
            "(output_bytes IS NULL AND output_preview IS NULL OR ?='codex') THEN 1 ELSE 0 END) "
            "FROM tool_calls "
            "WHERE session_id=? AND machine_id=? AND stream_id='main'",
            (row["agent"], sid, row["machine_id"]),
        ).fetchone()
        runs = conn.execute(
            "SELECT COUNT(*) FROM subagent_runs WHERE session_id=? AND machine_id=?",
            (sid, row["machine_id"]),
        ).fetchone()[0]
        head = (
            None
            if not outcome or outcome["base_head"] is None or outcome["head"] is None
            else int(outcome["base_head"] != outcome["head"])
        )
        values = {
            "user_turns": row["user_msg_count"] if messages else None,
            "wall_time_ms": row["duration_ms"],
            "input_tokens": inputs,
            "cached_input_tokens": cached,
            "cache_write_tokens": write,
            "output_tokens": output,
            "estimated_cost": cost,
            "peak_context_tokens": row["peak_context_tokens"],
            "compactions": row["compaction_count"] if messages else None,
            "tool_calls": calls if messages else None,
            "tool_errors": (errors or 0) if messages and not unknown_errors else None,
            "permission_wait_ms": session_hook_metrics(conn, sid)["permission_wait_ms"],
            "subagent_runs": runs if runs else None,
            "files_changed": outcome["files_changed"] if outcome else None,
            "insertions": outcome["insertions"] if outcome else None,
            "deletions": outcome["deletions"] if outcome else None,
            "head_moved": head,
            "outcome_label": label,
        }
        result.append(
            {
                "session_id": sid,
                "project": row["project"],
                "provider": row["agent"],
                "model": row["model"],
                "started_at": row["started_at"],
                "label": label,
                "values": values,
            }
        )
    return result


def _select(rows: list[dict], flt: dict) -> list[dict]:
    def matches(row: dict) -> bool:
        for key in ("project", "provider", "model", "label"):
            if key in flt and row[key] != flt[key]:
                return False
        if "session_ids" in flt and row["session_id"] not in flt["session_ids"]:
            return False
        day = row["started_at"][:10] if row["started_at"] else None
        return not (
            ("since" in flt and (day is None or day < flt["since"]))
            or ("until" in flt and (day is None or day > flt["until"]))
        )

    return [row for row in rows if matches(row)]


def compare(conn: sqlite3.Connection, pricing: Any, a: dict, b: dict, settings: Any) -> dict:
    rows = _records(conn, pricing, settings)
    first, second = _select(rows, a), _select(rows, b)
    metrics = []
    for name in _METRICS:
        av = [row["values"][name] for row in first]
        bv = [row["values"][name] for row in second]
        if name == "outcome_label":

            def categorical(values: list) -> dict:
                known = [value for value in values if value is not None]
                return {
                    "count": len(known),
                    "unknown": len(values) - len(known),
                    "median": None,
                    "mean": None,
                    "p90": None,
                    "categories": dict(sorted(Counter(known).items())),
                }

            left, right = categorical(av), categorical(bv)
        else:
            left, right = summarize(av), summarize(bv)
        delta = (
            right["median"] - left["median"]
            if left["median"] is not None and right["median"] is not None
            else None
        )
        metrics.append({"name": name, "a": left, "b": right, "difference": delta})
    return {
        "a_count": len(first),
        "b_count": len(second),
        "a_small": len(first) < 5,
        "b_small": len(second) < 5,
        "metrics": metrics,
    }
