"""Read-only overview of registered Watchdog projects."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from agent_watchdog import readapi
from wdi.metrics.pricing import Tariffs
from wdi.metrics.usage import usage


def overview(
    projects: list[readapi.RegisteredProject],
    *,
    since: datetime | None,
    until: datetime | None,
    tariffs: Tariffs | None = None,
) -> dict[str, Any]:
    """Keep unsupported and unavailable observations explicit per registration."""
    result: dict[str, Any] = dict(
        ok=True, complete=True, projects=[], observed_session_count=0, observed_event_count=0
    )
    for project in projects:
        row: dict[str, Any] = dict(
            project=project.name,
            root=project.root,
            database={"state": "unavailable"},
            session_count=None,
            event_count=None,
            usage=None,
            findings=None,
            last_activity=None,
        )
        result["projects"].append(row)
        if not (project.path / "events.sqlite3").is_file():
            result["complete"] = False
            continue
        try:
            with readapi.snapshot(project) as db:
                schema = db.execute("PRAGMA user_version").fetchone()[0]
                event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                sql = (
                    "SELECT COUNT(*) FROM (SELECT provider, session_id FROM events "
                    "WHERE session_id IS NOT NULL GROUP BY 1, 2)"
                    if schema >= 6
                    else "SELECT COUNT(*) FROM (SELECT json_extract(envelope, '$.provider'), "
                    "session_id "
                    "FROM events WHERE session_id IS NOT NULL GROUP BY 1, 2)"
                )
                session_count = db.execute(sql).fetchone()[0]
                last_activity = db.execute("SELECT MAX(received_at) FROM events").fetchone()[0]
            row.update(
                database={"state": "ready"}, event_count=event_count, session_count=session_count
            )
            result["observed_event_count"] += event_count
            result["observed_session_count"] += session_count
            if schema < 6:
                measured: dict[str, Any] = {"state": "unsupported", "reason": "schema_predates_v6"}
            else:
                full = usage(project, since=since, until=until, tariffs=tariffs, detailed=False)
                pricing = full["pricing"]
                measured = {
                    "state": "ready",
                    "token_usage": full["token_usage"],
                    "pricing": {
                        "currency": pricing["currency"],
                        "estimate": pricing["estimate"],
                        "caveat": pricing["caveat"],
                        "by_provider": pricing["by"]["provider"],
                    },
                }
            row.update(
                usage=measured, findings=readapi.findings(project), last_activity=last_activity
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            row["database"] = {"state": "error", "error": type(error).__name__}
            result["ok"] = False
        if any(
            row[name] is None or row[name]["state"] != "ready" for name in ("usage", "findings")
        ):
            result["complete"] = False
    return result
