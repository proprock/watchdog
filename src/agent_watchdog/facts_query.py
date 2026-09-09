"""Read-only aggregate queries over the schema-v6 ``event_facts`` table (WD-112).

Every figure is a raw ``SUM``/``COUNT`` of reported columns. Coverage (how many
rows actually carried a value) is reported next to the aggregates and never
folded into them, and no counter is ever a synthetic sum of the others. Callers
pass a read-only connection to a v6 database.
"""

import sqlite3
from typing import Any

_COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

# group_by -> (SELECT list with aliases, GROUP BY / ORDER BY expression)
_HOUR = "strftime('%Y-%m-%dT%H', received_at_us / 1000000, 'unixepoch')"
_DAY = "strftime('%Y-%m-%d', received_at_us / 1000000, 'unixepoch')"
_GROUPS: dict[str, tuple[str, str]] = {
    "provider": ("provider AS provider", "provider"),
    "model": ("model AS model", "model"),
    "effort": ("reasoning_effort AS reasoning_effort", "reasoning_effort"),
    "attribution": ("model_attribution AS model_attribution", "model_attribution"),
    "conversation": ("session_id AS session_id", "session_id"),
    "turn": ("session_id AS session_id, turn_id AS turn_id", "session_id, turn_id"),
    "hour": (f"{_HOUR} AS bucket", "bucket"),
    "day": (f"{_DAY} AS bucket", "bucket"),
}


def _rows(db: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = db.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _window(since_us: int | None, until_us: int | None) -> tuple[str, list[Any]]:
    clause, params = "", []
    if since_us is not None:
        clause += " AND received_at_us >= ?"
        params.append(since_us)
    if until_us is not None:
        clause += " AND received_at_us < ?"
        params.append(until_us)
    return clause, params


def _percentiles(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p90": None, "max": None}

    def at(pct: int) -> int:
        rank = max(1, (pct * len(ordered) + 99) // 100)
        return ordered[rank - 1]

    return {"p50": at(50), "p90": at(90), "max": ordered[-1]}


def token_usage(
    db: sqlite3.Connection,
    *,
    since_us: int | None = None,
    until_us: int | None = None,
    group_by: str = "provider",
) -> list[dict[str, Any]]:
    """Sum each raw token counter over ``kind='usage'`` rows, grouped one way."""
    if group_by not in _GROUPS:
        raise ValueError(f"Unsupported group_by: {group_by}")
    select_keys, group_expr = _GROUPS[group_by]
    where, params = _window(since_us, until_us)
    sums = ", ".join(f"SUM({name}) AS {name}" for name in _COUNTERS)
    coverage = ", ".join(f"SUM({name} IS NOT NULL) AS {name}_rows" for name in _COUNTERS)
    return _rows(
        db,
        f"SELECT {select_keys}, COUNT(*) AS usage_rows, {sums}, {coverage} "
        f"FROM event_facts WHERE kind = 'usage'{where} "
        f"GROUP BY {group_expr} ORDER BY {group_expr}",
        tuple(params),
    )


def _tool_cost(db: sqlite3.Connection, where: str, params: list[Any]) -> list[dict[str, Any]]:
    counts = _rows(
        db,
        "SELECT tool_name, "
        "COUNT(*) AS finishes, "
        "SUM(hook_event_name = 'PostToolUseFailure') AS errors, "
        "SUM(tool_duration_ms) AS wall_ms "
        f"FROM event_facts WHERE kind = 'tool.finish'{where} "
        "GROUP BY tool_name ORDER BY finishes DESC, tool_name",
        tuple(params),
    )
    durations: dict[str | None, list[int]] = {}
    for name, value in db.execute(
        "SELECT tool_name, tool_duration_ms FROM event_facts "
        f"WHERE kind = 'tool.finish' AND tool_duration_ms IS NOT NULL{where}",
        tuple(params),
    ).fetchall():
        durations.setdefault(name, []).append(value)
    for row in counts:
        row["errors"] = row["errors"] or 0
        row["error_rate"] = round(row["errors"] / row["finishes"], 4) if row["finishes"] else None
        row["duration_ms"] = _percentiles(durations.get(row["tool_name"], []))
    return counts


def _turn_tool_load(db: sqlite3.Connection, where: str, params: list[Any]) -> dict[str, Any]:
    per_turn = db.execute(
        "SELECT COUNT(*) AS tools, COALESCE(SUM(tool_duration_ms), 0) AS wall_ms "
        f"FROM event_facts WHERE kind = 'tool.finish' AND turn_id IS NOT NULL{where} "
        "GROUP BY session_id, turn_id",
        tuple(params),
    ).fetchall()
    tools = [row[0] for row in per_turn]
    wall = [row[1] for row in per_turn]
    return {
        "turns": len(per_turn),
        "tools_per_turn": _percentiles(tools),
        "tool_wall_ms_per_turn": _percentiles(wall),
    }


def _permission_throughput(
    db: sqlite3.Connection, where: str, params: list[Any]
) -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT permission_mode, "
        "SUM(kind = 'turn.start') AS turns, "
        "SUM(kind = 'tool.finish') AS tool_finishes, "
        "SUM(tool_duration_ms) AS tool_wall_ms "
        f"FROM event_facts WHERE permission_mode IS NOT NULL{where} "
        "GROUP BY permission_mode ORDER BY tool_finishes DESC, permission_mode",
        tuple(params),
    )


def _subagent_cost(db: sqlite3.Connection, where: str, params: list[Any]) -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT agent_type, "
        "SUM(kind = 'tool.finish') AS tool_finishes, "
        "SUM(CASE WHEN kind = 'tool.finish' THEN tool_duration_ms END) AS tool_wall_ms "
        f"FROM event_facts WHERE agent_id IS NOT NULL{where} "
        "GROUP BY agent_type ORDER BY tool_finishes DESC, agent_type",
        tuple(params),
    )


def _inter_turn_latency(db: sqlite3.Connection, where: str, params: list[Any]) -> dict[str, Any]:
    ordered = db.execute(
        "SELECT session_id, kind, received_at_us FROM event_facts "
        "WHERE kind IN ('turn.start', 'turn.end', 'agent.end') "
        f"AND received_at_us IS NOT NULL{where} "
        "ORDER BY session_id, received_at_us, rowid",
        tuple(params),
    ).fetchall()
    gaps_us: list[int] = []
    pending_end: dict[str, int] = {}
    for session_id, kind, at in ordered:
        if kind in ("turn.end", "agent.end"):
            pending_end[session_id] = at
        elif kind == "turn.start" and session_id in pending_end:
            gaps_us.append(at - pending_end.pop(session_id))
    return {
        "observed_gaps": len(gaps_us),
        "milliseconds": _percentiles([gap // 1000 for gap in gaps_us]),
    }


def _permission_stalls(db: sqlite3.Connection, where: str, params: list[Any]) -> dict[str, Any]:
    row = db.execute(
        "SELECT COUNT(*) FROM event_facts "
        f"WHERE kind = 'waiting' AND notification_type = 'permission_prompt'{where}",
        tuple(params),
    ).fetchone()
    return {"prompts": row[0]}


def process_efficiency(
    db: sqlite3.Connection, *, since_us: int | None = None, until_us: int | None = None
) -> dict[str, Any]:
    """Per-turn tool cost, autonomy, and human-in-the-loop projections."""
    where, params = _window(since_us, until_us)
    return {
        "tool_cost": _tool_cost(db, where, params),
        "turn_tool_load": _turn_tool_load(db, where, params),
        "permission_throughput": _permission_throughput(db, where, params),
        "permission_stalls": _permission_stalls(db, where, params),
        "inter_turn_latency": _inter_turn_latency(db, where, params),
        "subagent_cost": _subagent_cost(db, where, params),
    }


_COVERAGE_COLUMNS = (
    "turn_id",
    "occurred_at_us",
    "model",
    "reasoning_effort",
    "permission_mode",
    "tool_name",
    "tool_duration_ms",
    *_COUNTERS,
)
_ATTRIBUTED = {"model": "model_attribution", "reasoning_effort": "reasoning_effort_attribution"}


def coverage(db: sqlite3.Connection) -> dict[str, Any]:
    """Non-NULL counts per projected column, with attribution splits kept apart."""
    total = db.execute("SELECT COUNT(*) FROM event_facts").fetchone()[0]
    observed = _rows(
        db,
        "SELECT "
        + ", ".join(f"SUM({name} IS NOT NULL) AS {name}" for name in _COVERAGE_COLUMNS)
        + " FROM event_facts",
    )[0]
    result: dict[str, Any] = {"events": total}
    for name in _COVERAGE_COLUMNS:
        entry: dict[str, Any] = {"observed": observed[name] or 0, "total": total}
        if name in _ATTRIBUTED:
            entry["attribution"] = {
                state: count
                for state, count in db.execute(
                    f"SELECT {_ATTRIBUTED[name]}, COUNT(*) FROM event_facts GROUP BY 1"
                ).fetchall()
            }
        result[name] = entry
    return result
