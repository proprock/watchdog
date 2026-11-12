"""Tests for SQLite connection factory and migration runner."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from wdi.db.connection import apply_migrations, connect


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite")
    yield connection
    connection.close()


def test_connect_enables_foreign_keys_and_wal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connect_initializes_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "initialized.sqlite"
    with closing(connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        conn.execute("INSERT INTO machines (machine_id, created_at) VALUES ('m', 'now')")
        conn.execute(
            "INSERT INTO sessions (session_id, machine_id, agent) VALUES ('s', 'm', 'codex')"
        )
        conn.commit()
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT origin FROM sessions").fetchone()[0] == "file"


@pytest.mark.parametrize("revision", [0, 1, 3])
def test_connect_rejects_other_schemas_without_changing_them(tmp_path: Path, revision: int) -> None:
    path = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE old_data (value TEXT)")
        conn.execute(f"PRAGMA user_version = {revision}")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match=r"insights/scripts/rebuild_db\.py"):
        connect(path)

    assert path.read_bytes() == before


def test_apply_migrations_from_zero_creates_schema(conn: sqlite3.Connection) -> None:
    applied = apply_migrations(conn)

    assert applied >= 1
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {
        "machines",
        "projects",
        "project_paths",
        "sessions",
        "messages",
        "tool_calls",
        "subagent_runs",
        "import_state",
        "llm_analyses",
        "coach_reports",
    } <= tables

    session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert {"entrypoint", "web_search_requests", "web_fetch_requests", "is_noise"} <= session_cols
    assert {
        "parent_session_id",
        "turn_count",
        "turn_abort_count",
        "peak_context_tokens",
        "rate_limit_plan",
    } <= session_cols
    message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "stop_reason" in message_cols


def test_apply_migrations_is_idempotent(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    applied_again = apply_migrations(conn)

    assert applied_again == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (session_id, machine_id, agent) VALUES ('s1', 'ghost', 'claude')"
        )


def test_sessions_agent_check_constraint(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO machines (machine_id, created_at) VALUES ('m1', '2026-01-01T00:00:00+00:00')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (session_id, machine_id, agent) VALUES ('s1', 'm1', 'gemini')"
        )
