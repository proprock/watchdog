"""Tests for per-call context reconstruction and deterministic advice."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.context_analysis import (
    ContextAnalysisFilter,
    build_context_report,
    reconstruct_snapshots,
)
from wdi.db.connection import apply_migrations, connect
from wdi.db.repository import Repository
from wdi.ingest.context import ContextTracker, command_kind
from wdi.llm.context_advisor import build_context


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    apply_migrations(connection)
    Repository(connection).ensure_machine("m1")
    yield connection
    connection.close()


def event(idx: int, event_type: str, category: str, **values: object) -> dict[str, object]:
    return {
        "idx": idx,
        "ts": f"2026-06-01T10:00:{idx:02d}Z",
        "turn_id": "t1",
        "model_call_index": values.pop("model_call_index", 0),
        "event_type": event_type,
        "role": values.pop("role", None),
        "category": category,
        "char_count": values.pop("char_count", None),
        "byte_count": values.pop("byte_count", None),
        "fingerprint": values.pop("fingerprint", None),
        "tool_name": values.pop("tool_name", None),
        "command_kind": values.pop("command_kind", None),
        "target_fingerprint": values.pop("target_fingerprint", None),
        "input_tokens": values.pop("input_tokens", None),
        "output_tokens": values.pop("output_tokens", None),
        "cache_read_tokens": values.pop("cache_read_tokens", None),
        "cache_creation_tokens": values.pop("cache_creation_tokens", None),
        "reasoning_tokens": values.pop("reasoning_tokens", None),
        "context_window": values.pop("context_window", None),
        "compaction_generation": values.pop("compaction_generation", 0),
        "coverage": values.pop("coverage", "visible"),
        "extra": json.dumps(values.pop("extra", {})),
    }


def test_reconstruct_snapshots_replaces_instructions_and_handles_opaque_compact() -> None:
    rows = [
        event(
            0,
            "instruction",
            "instructions",
            byte_count=10,
            fingerprint="old",
            extra={"slot": "developer_instructions"},
        ),
        event(
            1, "model_call", "usage", input_tokens=100, cache_read_tokens=20, context_window=1000
        ),
        event(
            2,
            "instruction",
            "instructions",
            byte_count=6,
            fingerprint="new",
            model_call_index=1,
            extra={"slot": "developer_instructions"},
        ),
        event(
            3,
            "message",
            "assistant_text",
            byte_count=4,
            fingerprint="answer",
            model_call_index=1,
            extra={"direction": "output"},
        ),
        event(4, "model_call", "usage", input_tokens=200, model_call_index=1, context_window=1000),
        event(
            5,
            "compact_boundary",
            "opaque_compaction",
            model_call_index=2,
            compaction_generation=1,
            coverage="partial",
            extra={"trigger": "auto"},
        ),
        event(
            6,
            "message",
            "user_prompt",
            byte_count=3,
            fingerprint="new",
            model_call_index=2,
            compaction_generation=1,
            extra={"replacement": True},
        ),
        event(
            7,
            "opaque_compaction",
            "opaque_compaction",
            model_call_index=2,
            compaction_generation=1,
            coverage="opaque",
        ),
        event(
            8,
            "model_call",
            "usage",
            input_tokens=50,
            model_call_index=2,
            context_window=1000,
            compaction_generation=1,
        ),
    ]

    snapshots = reconstruct_snapshots(rows)

    assert [item.visible_bytes for item in snapshots] == [10, 6, 3]
    assert snapshots[1].categories == {"instructions": 6}
    assert snapshots[2].coverage == "partial"
    assert snapshots[2].repeated_bytes == 0
    assert snapshots[2].context_tokens == 50
    assert snapshots[2].utilization == 0.05


def test_opaque_output_only_affects_the_following_model_call() -> None:
    rows = [
        event(0, "message", "user_prompt", byte_count=8, fingerprint="prompt"),
        event(
            1,
            "opaque_reasoning",
            "opaque_reasoning",
            coverage="opaque",
            extra={"direction": "output"},
        ),
        event(2, "model_call", "usage", input_tokens=100),
        event(3, "model_call", "usage", input_tokens=120, model_call_index=1),
    ]

    snapshots = reconstruct_snapshots(rows)

    assert snapshots[0].coverage == "visible"
    assert snapshots[1].coverage == "partial"


def test_changed_target_is_separate_from_exact_repeat() -> None:
    rows = [
        event(
            0,
            "tool_output",
            "code_file",
            byte_count=10,
            fingerprint="version-one",
            target_fingerprint="same-file",
        ),
        event(1, "model_call", "usage", input_tokens=100),
        event(
            2,
            "tool_output",
            "code_file",
            byte_count=12,
            fingerprint="version-two",
            target_fingerprint="same-file",
            model_call_index=1,
        ),
        event(3, "model_call", "usage", input_tokens=120, model_call_index=1),
    ]

    snapshots = reconstruct_snapshots(rows)

    assert snapshots[1].repeated_bytes == 0
    assert snapshots[1].changed_target_bytes == 12


def test_compaction_pairing_only_deduplicates_codex_payload_markers() -> None:
    events = []
    tracker = ContextTracker(events)

    assert tracker.add_compaction("2026-06-01T10:00:00Z", trigger="auto") is True
    assert tracker.add_compaction("2026-06-01T10:00:01Z", trigger="manual") is True
    assert tracker.add_compaction("2026-06-01T10:00:02Z", payload=True) is True
    assert tracker.add_compaction("2026-06-01T10:00:03Z", marker=True) is False

    assert [item.event_type for item in events] == [
        "compact_boundary",
        "compact_boundary",
        "compact_boundary",
        "compact_marker",
    ]
    assert tracker.generation == 3


def test_command_kind_does_not_retain_shell_assignments_or_paths() -> None:
    assert command_kind("$env:API_KEY='secret'; uv run pytest") == "shell_expression"
    assert command_kind(r"C:\Tools\uv.exe run pytest") == "uv.exe"


def _seed_session(
    conn: sqlite3.Connection,
    session_id: str,
    agent: str,
    *,
    input_tokens: int,
    visible_bytes: int,
    compactions: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, agent, title, started_at, turn_count, input_tokens,
             peak_context_tokens, compaction_count)
        VALUES (?, 'm1', ?, ?, '2026-06-01T10:00:00Z', 2, ?, ?, ?)
        """,
        (session_id, agent, f"Session {session_id}", input_tokens, input_tokens, compactions),
    )
    conn.executemany(
        """
        INSERT INTO context_events
            (session_id, machine_id, stream_id, idx, event_type, category, byte_count,
             fingerprint, input_tokens, model_call_index, compaction_generation, coverage, extra)
        VALUES (?, ?, 'main', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        [
            (
                session_id,
                "m1",
                0,
                "message",
                "user_prompt",
                visible_bytes,
                f"hash-{session_id}",
                None,
                0,
                "visible",
                '{"direction":"input"}',
            ),
            (
                session_id,
                "m1",
                1,
                "model_call",
                "usage",
                None,
                None,
                input_tokens,
                0,
                "visible",
                None,
            ),
        ],
    )


def test_context_resume_uses_usage_events_for_the_next_call_index(
    conn: sqlite3.Connection,
) -> None:
    _seed_session(conn, "resume", "codex", input_tokens=100, visible_bytes=10)
    conn.execute(
        """
        INSERT INTO context_events
            (session_id, machine_id, stream_id, idx, model_call_index, event_type,
             category, byte_count, coverage, extra)
        VALUES ('resume', 'm1', 'main', 2, 1, 'message', 'assistant_text', 5,
                'visible', '{"direction":"output"}')
        """
    )
    conn.commit()

    _, next_call, _, _ = Repository(conn).context_resume("resume", "main")

    assert next_call == 1


def test_report_ranks_sessions_and_separates_providers(conn: sqlite3.Connection) -> None:
    _seed_session(conn, "small", "claude", input_tokens=100, visible_bytes=10)
    _seed_session(conn, "large", "codex", input_tokens=900, visible_bytes=100)
    conn.commit()

    report = build_context_report(
        conn,
        ContextAnalysisFilter(sort="input_tokens", top_n=1, min_calls=1),
    )

    assert [item.session_id for item in report.sessions] == ["large"]
    assert report.selected is not None
    assert report.selected.session_id == "large"
    assert {item.agent for item in report.providers} == {"claude", "codex"}
    assert report.total_sessions == 2


def test_report_returns_insufficient_evidence_below_advisor_threshold(
    conn: sqlite3.Connection,
) -> None:
    _seed_session(conn, "one", "claude", input_tokens=100, visible_bytes=10)
    conn.commit()

    report = build_context_report(conn, ContextAnalysisFilter())

    assert report.advice.strategy == "insufficient evidence"
    assert report.advice.revise_compact_prompt is False
    assert "Goal:" in report.advice.compact_prompt_draft


def test_llm_context_contains_only_aggregate_data(conn: sqlite3.Connection) -> None:
    _seed_session(conn, "private-session-id", "codex", input_tokens=100, visible_bytes=10)
    conn.commit()
    report = build_context_report(
        conn,
        ContextAnalysisFilter(),
    )

    payload = build_context(report)

    assert "private-session-id" not in payload
    assert "C:/private/project" not in payload
    assert "category_bytes" in payload
