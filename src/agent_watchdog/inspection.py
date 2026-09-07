"""Read-only project/session inspection; never open a storage writer."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID

from agent_watchdog import daemon, resources
from agent_watchdog.config import Project, UserPaths, load_config
from agent_watchdog.registry import Registry
from agent_watchdog.storage import StorageError, persisted_envelope


def project_at(paths: UserPaths, project_id: UUID | None) -> Project:
    config = load_config(paths.config)
    if project_id is None:
        resolution = Registry(config).resolve(Path.cwd())
        if resolution is not None:
            project_id = resolution.project_id
    project = next((item for item in config.projects if item.id == project_id), None)
    if project is None:
        raise StorageError(
            "Select a registered project with --project UUID or its working directory"
        )
    return project


@contextmanager
def database(paths: UserPaths, project: Project) -> Iterator[sqlite3.Connection]:
    path = paths.project_data(project.id) / "events.sqlite3"
    if not path.is_file():
        raise StorageError("Project has no collected database")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, 4):
            raise StorageError("Unsupported database schema")
        if db.execute("SELECT project_id FROM metadata").fetchall() != [(str(project.id),)]:
            raise StorageError("Database belongs to a different project")
        yield db


def _page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 1000 or offset < 0:
        raise StorageError("Require limit 1..1000 and a nonnegative offset")


def sessions(paths: UserPaths, project: Project, *, limit: int, offset: int) -> dict:
    _page(limit, offset)
    with database(paths, project) as db:
        rows = db.execute(
            "SELECT json_extract(envelope, '$.provider'), session_id, COUNT(*), "
            "MIN(received_at), MAX(received_at), SUM(kind='session.start'), "
            "SUM(kind='session.end'), SUM(kind='usage'), "
            "COUNT(DISTINCT json_extract(envelope, '$.checkout_id')) "
            "FROM events GROUP BY 1, 2 ORDER BY MAX(received_at) DESC, 1, 2 LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
    result = []
    for provider, session_id, count, first, last, started, ended, usage, checkouts in rows[:limit]:
        gaps = ["task_outcome_unknown"]
        if not usage:
            gaps.insert(0, "usage_not_enriched")
        if not started:
            gaps.append("session_start_not_observed")
        if not ended:
            gaps.append("session_end_not_observed")
        if session_id is None:
            gaps.append("session_identity_missing")
        result.append(
            {
                "provider": provider,
                "session_id": session_id,
                "event_count": count,
                "first_received_at": first,
                "last_received_at": last,
                "checkout_count": checkouts,
                "task_outcome": "unknown",
                "gaps": gaps,
            }
        )
    return {"project_id": str(project.id), "sessions": result, "has_more": len(rows) > limit}


def show(
    paths: UserPaths,
    project: Project,
    session_id: str | None,
    *,
    provider: str,
    limit: int,
    offset: int,
) -> dict:
    _page(limit, offset)
    with database(paths, project) as db:
        condition = "session_id IS ? AND json_extract(envelope, '$.provider')=?"
        count = db.execute(
            f"SELECT COUNT(*) FROM events WHERE {condition}", (session_id, provider)
        ).fetchone()[0]
        if not count:
            raise StorageError("Unknown session in the selected project/provider")
        rows = db.execute(
            f"SELECT envelope FROM events WHERE {condition} ORDER BY rowid LIMIT ? OFFSET ?",
            (session_id, provider, limit + 1, offset),
        ).fetchall()
        events = [persisted_envelope(row[0]).model_dump(mode="json") for row in rows[:limit]]
    return {
        "project_id": str(project.id),
        "provider": provider,
        "session_id": session_id,
        "event_count": count,
        "events": events,
        "has_more": len(rows) > limit,
        "task_outcome": "unknown",
    }


def report(
    paths: UserPaths,
    project: Project,
    *,
    session_id: str | None,
    provider: str,
) -> dict:
    """Analyze a consistent read-only event snapshot for one provider/session."""
    from agent_watchdog.analysis import analyze

    with database(paths, project) as db:
        condition = "json_extract(envelope, '$.provider')=?"
        parameters: list[object] = [provider]
        if session_id is not None:
            condition += " AND session_id=?"
            parameters.append(session_id)
        rows = db.execute(
            f"SELECT envelope FROM events WHERE {condition} ORDER BY rowid", parameters
        ).fetchall()
        if session_id is not None and not rows:
            raise StorageError("Unknown session in the selected project/provider")
        snapshots: list[dict[str, object]] = []
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diff_snapshots'"
        ).fetchone()
        if table is not None:
            checkouts = {
                event.checkout_id
                for event in (persisted_envelope(row[0]) for row in rows)
                if event.checkout_id is not None
            }
            if checkouts:
                placeholders = ",".join("?" for _ in checkouts)
                snapshots = [
                    {
                        "snapshot_id": snapshot_id,
                        "checkout_id": checkout_id,
                        "fingerprint": fingerprint,
                    }
                    for snapshot_id, checkout_id, fingerprint in db.execute(
                        "SELECT snapshot_id, checkout_id, fingerprint FROM diff_snapshots "
                        f"WHERE checkout_id IN ({placeholders}) ORDER BY observed_at, rowid",
                        tuple(str(checkout) for checkout in checkouts),
                    )
                ]
    result = analyze((persisted_envelope(row[0]) for row in rows), snapshots=snapshots)
    return {"project_id": str(project.id), "provider": provider, **result}


def doctor(paths: UserPaths) -> dict:
    config = load_config(paths.config)
    projects = []
    healthy = True
    for project in config.projects:
        root = paths.project_data(project.id)
        state: dict = {"state": "unavailable"}
        try:
            if (root / "events.sqlite3").exists():
                with database(paths, project) as db:
                    state = {
                        "state": "ready",
                        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                        "event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    }
            counters = resources.losses(root)
            used = resources.usage(root)
            capacity = (
                resources.available(root, project.overrides.apply(config.defaults))
                if root.exists()
                else None
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            state = {"state": "error", "error": type(error).__name__}
            counters, used, capacity = None, None, None
        exists = project.root.is_dir()
        healthy &= state["state"] != "error" and exists and (capacity is None or capacity >= 65536)
        projects.append(
            {
                "id": str(project.id),
                "root_exists": exists,
                "database": state,
                "losses": counters,
                "used_bytes": used,
                "available_bytes": capacity,
            }
        )
    core = daemon.status(paths)
    healthy &= core["state"] != "degraded"
    return {
        "ok": healthy,
        "projects": projects,
        "daemon": core,
        "gaps": [
            "native_hook_trust_not_inspected",
            "provider_event_coverage_not_inferred",
            "usage_not_enriched",
        ],
    }
