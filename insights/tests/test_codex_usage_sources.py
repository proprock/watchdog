"""Characterize the independent Codex collector and analytics usage sources."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.ingest.codex import import_codex_source, parse_codex_lines
from wdi.ingest.projects import ProjectResolver

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich

SID = "019eae50-c169-73c1-81ba-eeba321f3026"
MACHINE = "synthetic-machine"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "insights.sqlite")
    yield connection
    connection.close()


def record(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": "2026-06-09T20:00:00Z", "type": kind, "payload": payload}


def counters(input_tokens: int, cached: int, output: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
    }


def notification(input_tokens: int, cached: int, output: int) -> dict[str, Any]:
    usage = counters(input_tokens, cached, output)
    return record(
        "event_msg",
        {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}},
    )


def native(
    response: str,
    input_tokens: int,
    cached: int,
    output: int,
    *,
    response_usage: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    usage = counters(input_tokens, cached, output)
    last = counters(*response_usage) if response_usage is not None else usage
    return record(
        "token_usage_record",
        {
            "session_id": SID,
            "thread_id": SID,
            "turn_id": "t1",
            "root_turn_id": "t1",
            "response_id": response,
            "usage": last,
            "turn_token_usage": last,
            "thread_token_usage": usage,
        },
    )


def prefix(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        record(
            "session_meta",
            {"id": SID, "cli_version": "0.153.4", "cwd": str(tmp_path / "project-a")},
        ),
        record("turn_context", {"turn_id": "t1", "model": "gpt-5.4"}),
        notification(100, 80, 5),
        native("r1", 100, 80, 5),
    ]


def encode(records: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(item) + "\n" for item in records).encode("utf-8")


def test_compaction_native_usage_and_notifications_have_different_totals(tmp_path: Path) -> None:
    raw = encode(
        [
            *prefix(tmp_path),
            native("r2", 130, 100, 12, response_usage=(30, 20, 7)),
            record("compacted", {"compaction_response_id": "r2"}),
        ]
    )
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(raw)
    project = uuid4()
    with Store(tmp_path / "collector", project) as store:
        store.put(
            Envelope(
                provider="codex",
                project_id=project,
                session_id=SID,
                kind="turn.end",
                source="hook",
                payload={"codex": {"transcript_path": str(path)}},
            )
        )
        result = enrich(store)
        assert result.failures == ()
        deltas = []
        for event in store.events():
            if event.kind != "usage":
                continue
            codex = event.payload["codex"]
            assert isinstance(codex, dict)
            usage = codex["usage"]
            assert isinstance(usage, dict)
            delta = usage["delta"]
            assert isinstance(delta, dict)
            deltas.append(delta)
        assert len(deltas) == 2
        assert tuple(
            sum(delta[name] for delta in deltas)
            for name in ("input_tokens", "cached_input_tokens", "output_tokens")
        ) == (130, 100, 12)

    chunk = parse_codex_lines(raw.splitlines(keepends=True), msg_idx0=0, tool_idx0=0)
    assert tuple(
        sum(getattr(message, name) or 0 for message in chunk.messages)
        for name in ("input_tokens", "cache_read_tokens", "output_tokens")
    ) == (100, 80, 5)
    assert all(message.cache_creation_tokens is None for message in chunk.messages)
    assert path.read_bytes() == raw


def test_incremental_import_catches_up_without_duplicating_usage(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    resolver = ProjectResolver(repo, MACHINE, aliases={})
    root = tmp_path / "codex"
    root.mkdir()
    path = root / f"rollout-2026-06-09T20-00-00-{SID}.jsonl"
    initial = encode(prefix(tmp_path))
    path.write_bytes(initial)

    assert import_codex_source(repo, resolver, root, MACHINE).files_imported == 1
    row = conn.execute(
        "SELECT input_tokens, cache_read_tokens, output_tokens FROM sessions WHERE session_id = ?",
        (SID,),
    ).fetchone()
    assert tuple(row) == (100, 80, 5)
    assert path.read_bytes() == initial

    appended = encode(
        [
            record("turn_context", {"turn_id": "t2", "model": "gpt-5.4"}),
            notification(40, 20, 3),
            native("r2", 140, 100, 8, response_usage=(40, 20, 3)),
        ]
    )
    with path.open("ab") as stream:
        stream.write(appended)
    assert import_codex_source(repo, resolver, root, MACHINE).files_imported == 1
    row = conn.execute(
        "SELECT input_tokens, cache_read_tokens, output_tokens FROM sessions WHERE session_id = ?",
        (SID,),
    ).fetchone()
    assert tuple(row) == (140, 100, 8)
    assert import_codex_source(repo, resolver, root, MACHINE).files_imported == 0
    repeated = conn.execute(
        "SELECT input_tokens, cache_read_tokens, output_tokens FROM sessions WHERE session_id = ?",
        (SID,),
    ).fetchone()
    assert tuple(repeated) == tuple(row)
    assert (
        conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (SID,)).fetchone()[0]
        == 2
    )
    state = repo.get_import_state(MACHINE, str(path))
    assert state is not None
    assert state.byte_offset == len(initial + appended)
    assert path.read_bytes() == initial + appended


def test_repeated_cumulative_notification_is_distinct_in_another_turn(tmp_path: Path) -> None:
    raw = encode(
        [
            *prefix(tmp_path),
            notification(100, 80, 5),
            record("turn_context", {"turn_id": "t2", "model": "gpt-5.4"}),
            notification(100, 80, 5),
        ]
    )
    chunk = parse_codex_lines(raw.splitlines(keepends=True), msg_idx0=0, tool_idx0=0)
    assert tuple(
        sum(getattr(message, name) or 0 for message in chunk.messages)
        for name in ("input_tokens", "cache_read_tokens", "output_tokens")
    ) == (200, 160, 10)
    calls = [event for event in chunk.context_events if event.event_type == "model_call"]
    duplicates = [
        event for event in chunk.context_events if event.event_type == "usage_observation"
    ]
    assert len(calls) == 2
    assert len(duplicates) == 1
    assert duplicates[0].extra is not None
    assert duplicates[0].extra["duplicate_of"] == calls[0].idx
    assert calls[0].extra is not None
    assert calls[0].extra["merged_observations"] == 2
    for event in [*calls, *duplicates]:
        assert event.extra is not None
        assert event.extra["raw_usage"] == {
            "input_tokens": 100,
            "cache_read_tokens": 80,
            "cache_creation_tokens": None,
            "output_tokens": 5,
            "reasoning_tokens": None,
        }
    assert chunk.messages[1].extra is not None
    assert chunk.messages[1].extra["duplicate_usage"] is True
