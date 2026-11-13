"""Synthetic source fixtures for WD-198 reporting parity."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store, canonical

V5_DDL = (
    "CREATE TABLE metadata (project_id TEXT NOT NULL)",
    "CREATE TABLE events (event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
    "session_id TEXT, kind TEXT NOT NULL, envelope TEXT NOT NULL)",
    "CREATE INDEX events_session ON events(session_id, received_at)",
    "CREATE TABLE artifacts (event_id TEXT NOT NULL REFERENCES events(event_id), "
    "name TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, "
    "PRIMARY KEY(event_id, name))",
    "CREATE TABLE pins (session_id TEXT PRIMARY KEY)",
    "CREATE TABLE receipts (event_id TEXT PRIMARY KEY REFERENCES events(event_id), "
    "digest TEXT NOT NULL)",
    "CREATE TABLE transcript_sources ("
    "provider TEXT NOT NULL, session_id TEXT NOT NULL, path TEXT NOT NULL, "
    "last_seen TEXT NOT NULL, reader TEXT, device INTEGER, inode INTEGER, "
    "size INTEGER, mtime INTEGER, offset INTEGER NOT NULL DEFAULT 0, "
    "tail BLOB NOT NULL DEFAULT X'', counters TEXT, last_error TEXT, "
    "error_signature TEXT, PRIMARY KEY(provider, session_id, path))",
    "CREATE TABLE diff_snapshots ("
    "snapshot_id TEXT PRIMARY KEY, checkout_id TEXT NOT NULL, "
    "fingerprint TEXT NOT NULL, byte_count INTEGER NOT NULL, observed_at TEXT NOT NULL)",
    "CREATE INDEX diff_snapshots_checkout ON diff_snapshots(checkout_id, observed_at)",
    "CREATE TABLE session_labels ("
    "provider TEXT NOT NULL, session_id TEXT NOT NULL, task_outcome TEXT NOT NULL, "
    "task_type TEXT, PRIMARY KEY(provider, session_id))",
)

BASE = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def build_v5(path, project_id, envelopes):
    """Write a v5 database whose ``events`` rows are the given envelopes."""
    db = sqlite3.connect(path / "events.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("BEGIN")
        for statement in V5_DDL:
            db.execute(statement)
        db.execute("INSERT INTO metadata VALUES (?)", (str(project_id),))
        for event in envelopes:
            document = canonical(event)
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    str(event.event_id),
                    event.received_at.isoformat(),
                    event.session_id,
                    event.kind,
                    document,
                ),
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, ?)",
                (str(event.event_id), hashlib.sha256(document.encode()).hexdigest()),
            )
        db.execute("PRAGMA user_version=5")
        db.execute("COMMIT")
    finally:
        db.close()


def claude_hook(project, *, session, kind, offset, prompt_id, effort=None, **metadata):
    md = {"hook_event_name": "PostToolUse", "session_id": session, "prompt_id": prompt_id}
    if effort is not None:
        md["effort"] = {"level": effort}
    md.update(metadata)
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={"claude": {"metadata": md, "content": "omitted"}},
        availability={},
    )


def claude_usage(project, *, session, offset, request_id, model, response):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        kind="usage",
        source="transcript",
        native_event_id=request_id,
        occurred_at=BASE + timedelta(seconds=offset),
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "claude": {
                "reader": "claude-transcript-v1",
                "model": model,
                "request_id": request_id,
                "usage": {"response": response},
            }
        },
        availability={},
    )


def codex_hook(project, *, session, kind, offset, turn_id, model=None):
    md = {"hook_event_name": "PostToolUse", "session_id": session, "turn_id": turn_id}
    if model is not None:
        md["model"] = model
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        turn_id=turn_id,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={"codex": {"metadata": md, "content": "omitted"}},
        availability={},
    )


def codex_usage(project, *, session, offset, response_id, turn_id, delta):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        turn_id=turn_id,
        kind="usage",
        source="transcript",
        native_event_id=response_id,
        occurred_at=BASE + timedelta(seconds=offset),
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "codex": {
                "reader": "codex-rollout-v1",
                "response_id": response_id,
                "thread_id": session,
                "usage": {"cumulative": delta, "delta": delta},
            }
        },
        availability={},
    )


SESSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

SESSION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def sample_events(project):
    return [
        codex_hook(
            project, session=SESSION_A, kind="turn.start", offset=0, turn_id="t1", model="gpt-5.6"
        ),
        codex_hook(
            project, session=SESSION_A, kind="tool.start", offset=1, turn_id="t1", model="gpt-5.6"
        ),
        codex_hook(
            project, session=SESSION_A, kind="tool.finish", offset=3, turn_id="t1", model="gpt-5.6"
        ),
        codex_usage(
            project,
            session=SESSION_A,
            offset=4,
            response_id="r-a1",
            turn_id="t1",
            delta={
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 0,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
            },
        ),
        claude_hook(
            project,
            session=SESSION_B,
            kind="tool.finish",
            offset=10,
            prompt_id="pp-1",
            effort="high",
            permission_mode="auto",
            tool_name="Bash",
            tool_use_id="u-1",
            duration_ms=250,
        ),
        claude_hook(
            project,
            session=SESSION_B,
            kind="tool.finish",
            offset=12,
            prompt_id="pp-1",
            effort="high",
            permission_mode="auto",
            tool_name="Bash",
            tool_use_id="u-2",
            duration_ms=90,
            hook_event_name="PostToolUseFailure",
        ),
        claude_usage(
            project,
            session=SESSION_B,
            offset=13,
            request_id="req-b1",
            model="claude-sonnet-5",
            response={
                "input_tokens": 3,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 100,
                "output_tokens": 400,
                "thinking_tokens": 50,
            },
        ),
    ]


def seed(root, project):
    root.mkdir(parents=True, exist_ok=True)
    build_v5(root, project, sample_events(project))
    with Store(root, project):
        pass  # reopen migrates v5 -> v6 and backfills event_facts


TARIFFS = """\
currency = "USD"

["2026-01-01"]
"claude-sonnet-5" = { input = "3.00", output = "15.00", cache_read = "0.30", cache_write = "3.75" }
"gpt-5.6" = { input = "1.25", output = "10.00", cache_read = "0.125", cache_write = "0" }

["2026-09-09"]
"claude-sonnet-5" = { input = "2.00", output = "10.00", cache_read = "0.20", cache_write = "2.50" }
"gpt-5.6" = { input = "1.25", output = "10.00", cache_read = "0.125", cache_write = "0" }
"""

T0 = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _finish(
    project_id,
    *,
    at: int,
    tool_name: str = "Bash",
    failed: bool = False,
    error: str | None = None,
    command: str = "uv run pytest",
    session_id: str = "s1",
    agent_id: str | None = None,
    provider: str = "claude",
) -> Envelope:
    content: dict = {"tool_input": {"command": command}}
    if not failed:
        content["tool_response"] = {"stdout": "ok", "stderr": ""}
    return Envelope(
        provider=provider,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        kind="tool.finish",
        source="hook",
        received_at=T0 + timedelta(seconds=at),
        payload={
            provider: {
                "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
                "tool_name": tool_name,
                "content": content,
                "metadata": {"error": error} if error is not None else {},
            }
        },
    )


def registered(paths, project):
    from agent_watchdog.readapi import RegisteredProject

    return RegisteredProject(
        project_id=str(project.id),
        name=project.root.name,
        root=str(project.root),
        path=paths.project_data(project.id),
    )


def cli(tmp_path, command, *args):
    from typer.testing import CliRunner
    from wdi.cli import app

    result = CliRunner().invoke(
        app,
        [
            command,
            "--watchdog-config",
            str(tmp_path / "config.toml"),
            "--watchdog-data",
            str(tmp_path / "data"),
            *args,
        ],
    )
    return result.exit_code, json.loads(result.stdout)
