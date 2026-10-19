"""Shared read-only snapshot and parsed event cache for insight builders."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.execution import normalize_events

_ACTIVE: ContextVar[tuple[UserPaths, Project, sqlite3.Connection] | None] = ContextVar(
    "insight_snapshot", default=None
)
_PARSED: ContextVar[dict[str, object] | None] = ContextVar("insight_parsed_envelopes", default=None)
_NORMALIZED: ContextVar[dict[tuple[str | None, str | None], list[Envelope]] | None] = ContextVar(
    "insight_normalized_events", default=None
)
_PROJECTIONS: ContextVar[dict[tuple[Any, ...], Any] | None] = ContextVar(
    "insight_query_projections", default=None
)


@contextmanager
def database(paths: UserPaths, project: Project) -> Iterator[sqlite3.Connection]:
    """Reuse the digest snapshot when nested; otherwise open a normal read snapshot."""
    active = _ACTIVE.get()
    if active is not None and active[:2] == (paths, project):
        yield active[2]
        return
    with inspection.database(paths, project) as db:
        token = _ACTIVE.set((paths, project, db))
        parsed_token = _PARSED.set({})
        normalized_token = _NORMALIZED.set({})
        projections_token = _PROJECTIONS.set({})
        try:
            yield db
        finally:
            _PROJECTIONS.reset(projections_token)
            _NORMALIZED.reset(normalized_token)
            _PARSED.reset(parsed_token)
            _ACTIVE.reset(token)


def persisted(document: str):
    """Parse a persisted envelope once in this window, or parse it standalone."""
    from agent_watchdog.storage import persisted_envelope

    cache = _PARSED.get()
    if cache is None:
        return persisted_envelope(document)
    if document not in cache:
        cache[document] = persisted_envelope(document)
    return cache[document]


def normalized_events(
    db: sqlite3.Connection, provider: str | None, session_id: str | None
) -> list[Envelope] | None:
    """Return one normalized project event projection during a digest snapshot."""
    active = _ACTIVE.get()
    cache = _NORMALIZED.get()
    if active is None or active[2] is not db or cache is None:
        return None
    key = provider, session_id
    if key not in cache:
        cache[key] = normalize_events(source_events(db, provider, session_id))
    return cache[key]


def source_events(
    db: sqlite3.Connection, provider: str | None, session_id: str | None
) -> list[Envelope]:
    """Share raw namespace evidence and normalization from the same event query."""

    def load() -> list[Envelope]:
        where, params = "1 = 1", []
        if provider is not None:
            where += " AND provider = ?"
            params.append(provider)
        if session_id is not None:
            where += " AND session_id = ?"
            params.append(session_id)
        documents = db.execute(
            f"SELECT envelope FROM events WHERE {where} ORDER BY received_at", params
        )
        return [persisted(document) for (document,) in documents]

    return projection(db, ("source-events", provider, session_id), load)


def projection(db: sqlite3.Connection, key: tuple[Any, ...], load: Callable[[], Any]) -> Any:
    """Cache a common query result for this digest snapshot only."""
    cache = _PROJECTIONS.get()
    active = _ACTIVE.get()
    if cache is None or active is None or active[2] is not db:
        return load()
    if key not in cache:
        cache[key] = load()
    return cache[key]


def tool_finishes(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[dict[str, str], list[Envelope]]:
    """Read agent labels and normalized finishes from the current project snapshot."""
    from agent_watchdog.insights import timeline

    with database(paths, project) as db:
        events = timeline.events(
            db,
            ("agent.start", "tool.finish"),
            provider=provider,
            session_id=session_id,
            # Agent labels and yielded-call context may begin outside the report window.
            since=None,
            until=None,
        )
    labels: dict[str, str] = {}
    finishes = []
    for event in events:
        if event.kind == "agent.start":
            agent_type = timeline.metadata(event).get("agent_type")
            if event.agent_id is not None and isinstance(agent_type, str):
                labels[event.agent_id] = agent_type
        elif (
            event.kind == "tool.finish"
            and (since is None or event.received_at >= since)
            and (until is None or event.received_at < until)
        ):
            finishes.append(event)
    return labels, finishes
