"""Tests for the repository layer."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.ingest.models import ToolCallRec, ToolResultRec


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repository]:
    conn = connect(tmp_path / "test.sqlite")
    yield Repository(conn)
    conn.close()


def test_ensure_machine_is_idempotent(repo: Repository) -> None:
    repo.ensure_machine("m1", hostname="host-a", os_name="win32")
    repo.ensure_machine("m1", hostname="host-a", os_name="win32")

    rows = repo.conn.execute("SELECT machine_id, hostname FROM machines").fetchall()
    assert len(rows) == 1
    assert rows[0]["hostname"] == "host-a"


def test_get_or_create_project_returns_same_id(repo: Repository) -> None:
    first = repo.get_or_create_project("my-app")
    second = repo.get_or_create_project("my-app")
    other = repo.get_or_create_project("other-app")

    assert first == second
    assert first != other


def test_link_project_path_is_idempotent(repo: Repository) -> None:
    repo.ensure_machine("m1")
    project_id = repo.get_or_create_project("my-app")

    repo.link_project_path(project_id, "m1", "C:/Repos/my-app")
    repo.link_project_path(project_id, "m1", "C:/Repos/my-app")

    count = repo.conn.execute("SELECT COUNT(*) FROM project_paths").fetchone()[0]
    assert count == 1


def test_import_state_roundtrip(repo: Repository) -> None:
    repo.ensure_machine("m1")

    assert repo.get_import_state("m1", "a/b.jsonl") is None

    repo.set_import_state("m1", "a/b.jsonl", file_size=100, file_mtime=1.5, byte_offset=90)
    state = repo.get_import_state("m1", "a/b.jsonl")
    assert state is not None
    assert state.file_size == 100
    assert state.byte_offset == 90

    repo.set_import_state("m1", "a/b.jsonl", file_size=200, file_mtime=2.5, byte_offset=180)
    state = repo.get_import_state("m1", "a/b.jsonl")
    assert state is not None
    assert state.file_size == 200
    assert state.byte_offset == 180


def test_reset_import_state_clears_all(repo: Repository) -> None:
    repo.ensure_machine("m1")
    repo.set_import_state("m1", "a.jsonl", file_size=1, file_mtime=1.0, byte_offset=1)
    repo.set_import_state("m1", "b.jsonl", file_size=2, file_mtime=2.0, byte_offset=2)

    repo.reset_import_state("m1")

    assert repo.get_import_state("m1", "a.jsonl") is None
    assert repo.get_import_state("m1", "b.jsonl") is None


def _insert_call(repo: Repository, call_id: str, ts: str | None) -> None:
    repo.ensure_machine("m1")
    repo.ensure_session("s1", "m1", "claude")
    repo.insert_tool_calls(
        "s1",
        "m1",
        "main",
        [ToolCallRec(idx=0, tool_name="Bash", category="exec", call_id=call_id, ts=ts)],
    )


def test_apply_tool_result_derives_duration_from_ts_delta(repo: Repository) -> None:
    _insert_call(repo, "c1", "2026-06-01T10:00:00Z")
    repo.apply_tool_result("s1", ToolResultRec(call_id="c1", ts="2026-06-01T10:00:03.500Z"))

    row = repo.conn.execute("SELECT duration_ms FROM tool_calls WHERE call_id = 'c1'").fetchone()
    assert row["duration_ms"] == 3500


def test_apply_tool_result_explicit_duration_wins(repo: Repository) -> None:
    _insert_call(repo, "c2", "2026-06-01T10:00:00Z")
    repo.apply_tool_result(
        "s1", ToolResultRec(call_id="c2", duration_ms=42, ts="2026-06-01T10:00:03Z")
    )

    row = repo.conn.execute("SELECT duration_ms FROM tool_calls WHERE call_id = 'c2'").fetchone()
    assert row["duration_ms"] == 42


def test_apply_tool_result_without_ts_keeps_duration_null(repo: Repository) -> None:
    _insert_call(repo, "c3", "2026-06-01T10:00:00Z")
    repo.apply_tool_result("s1", ToolResultRec(call_id="c3"))

    row = repo.conn.execute("SELECT duration_ms FROM tool_calls WHERE call_id = 'c3'").fetchone()
    assert row["duration_ms"] is None


def test_apply_tool_result_first_writer_keeps_exec_values(repo: Repository) -> None:
    """exec_command_end telemetry (precise, arrives first) must not be clobbered
    by the coarser values parsed from the function_call_output header."""
    _insert_call(repo, "c4", "2026-06-01T10:00:00Z")
    repo.apply_tool_result(
        "s1", ToolResultRec(call_id="c4", is_error=True, exit_code=1, duration_ms=2500)
    )
    repo.apply_tool_result(
        "s1", ToolResultRec(call_id="c4", is_error=True, exit_code=1, duration_ms=2900)
    )

    row = repo.conn.execute(
        "SELECT exit_code, duration_ms, is_error FROM tool_calls WHERE call_id = 'c4'"
    ).fetchone()
    assert row["exit_code"] == 1
    assert row["duration_ms"] == 2500
    assert row["is_error"] == 1


def test_apply_tool_result_category_override(repo: Repository) -> None:
    _insert_call(repo, "c5", "2026-06-01T10:00:00Z")
    repo.apply_tool_result("s1", ToolResultRec(call_id="c5", category="mcp"))
    repo.apply_tool_result("s1", ToolResultRec(call_id="c5", output_preview="late output"))

    row = repo.conn.execute("SELECT category FROM tool_calls WHERE call_id = 'c5'").fetchone()
    assert row["category"] == "mcp"


def test_recompute_peak_context_tokens_claude_shape(repo: Repository) -> None:
    from wdi.ingest.models import MessageRec

    repo.ensure_machine("m1")
    repo.ensure_session("s2", "m1", "claude")
    repo.insert_messages(
        "s2",
        "m1",
        "main",
        [
            MessageRec(
                idx=0,
                role="assistant",
                input_tokens=100,
                cache_read_tokens=1000,
                cache_creation_tokens=200,
            ),
            MessageRec(
                idx=1,
                role="assistant",
                input_tokens=50,
                cache_read_tokens=2000,
                cache_creation_tokens=0,
            ),
            MessageRec(idx=2, role="user"),
        ],
    )
    repo.recompute_session_aggregates("s2")

    row = repo.conn.execute(
        "SELECT peak_context_tokens FROM sessions WHERE session_id = 's2'"
    ).fetchone()
    assert row["peak_context_tokens"] == 2050


def test_recompute_preserves_claude_web_search_requests(repo: Repository) -> None:
    repo.ensure_machine("m1")
    repo.ensure_session("s3", "m1", "claude")
    repo.set_session_fields("s3", {"web_search_requests": 5})
    repo.recompute_session_aggregates("s3")

    row = repo.conn.execute(
        "SELECT web_search_requests FROM sessions WHERE session_id = 's3'"
    ).fetchone()
    assert row["web_search_requests"] == 5


def test_repository_rejects_unmigrated_db(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "raw.sqlite")
    try:
        repo = Repository(conn)
        with pytest.raises(sqlite3.OperationalError):
            repo.ensure_machine("m1")
    finally:
        conn.close()
