"""Exercise the one-off database transfer scripts with synthetic sessions."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from wdi.db.connection import connect

_INSIGHTS = Path(__file__).resolve().parents[1]
_SCHEMA = _INSIGHTS / "src" / "wdi" / "db" / "migrations" / "0001_init.sql"


def _connect_fixture(path: Path) -> sqlite3.Connection:
    # The production connection deliberately rejects old revisions.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_database(path: Path, *, old_schema: bool = False) -> sqlite3.Connection:
    conn = _connect_fixture(path)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if old_schema and "origin" in columns:
        conn.execute("ALTER TABLE sessions DROP COLUMN origin")
    elif not old_schema and "origin" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'file'")
    conn.execute("PRAGMA user_version = 1" if old_schema else "PRAGMA user_version = 2")
    return conn


def _add_machine(conn: sqlite3.Connection, machine_id: str) -> None:
    conn.execute("INSERT INTO machines (machine_id, created_at) VALUES (?, 'date')", (machine_id,))


def _add_session(
    conn: sqlite3.Connection, session_id: str, machine_id: str, project_id: int, title: str
) -> None:
    conn.execute(
        """INSERT INTO sessions
               (session_id, machine_id, project_id, agent, source_path, title, input_tokens)
           VALUES (?, ?, ?, 'codex', 'missing.jsonl', ?, 41)""",
        (session_id, machine_id, project_id, title),
    )
    conn.execute(
        """INSERT INTO messages (session_id, machine_id, idx, role, content_preview)
           VALUES (?, ?, 0, 'user', ?)""",
        (session_id, machine_id, title),
    )
    conn.execute(
        """INSERT INTO tool_calls (session_id, machine_id, idx, tool_name, target)
           VALUES (?, ?, 0, 'test-tool', ?)""",
        (session_id, machine_id, title),
    )
    conn.execute(
        """INSERT INTO context_events
               (session_id, machine_id, idx, event_type, category, fingerprint)
           VALUES (?, ?, 0, 'message', 'user', ?)""",
        (session_id, machine_id, title),
    )
    conn.execute(
        """INSERT INTO subagent_runs (agent_run_id, session_id, machine_id, prompt)
           VALUES (?, ?, ?, ?)""",
        (session_id + "-worker", session_id, machine_id, title),
    )


def _add_analysis(conn: sqlite3.Connection, session_id: str, machine_id: str, result: str) -> None:
    conn.execute(
        """INSERT INTO llm_analyses
               (session_id, machine_id, analysis_type, prompt_version, model, created_at, result)
           VALUES (?, ?, 'summary', 'v1', 'model', 'date', ?)""",
        (session_id, machine_id, result),
    )


def _add_coach(conn: sqlite3.Connection, filter_hash: str, result: str) -> None:
    conn.execute(
        """INSERT INTO coach_reports (filter_hash, prompt_version, model, created_at, result)
           VALUES (?, 'v1', 'model', 'date', ?)""",
        (filter_hash, result),
    )


def _run_script(script: str, db: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(_INSIGHTS / "scripts" / script), "--db", str(db), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _assert_transcripts(conn: sqlite3.Connection, expected: dict[str, str]) -> None:
    for query in (
        "SELECT session_id, content_preview FROM messages",
        "SELECT session_id, target FROM tool_calls",
        "SELECT session_id, fingerprint FROM context_events",
        "SELECT session_id, prompt FROM subagent_runs",
    ):
        assert sorted(map(tuple, conn.execute(query))) == sorted(expected.items())


def test_import_legacy_database_preserves_existing_sessions_on_repeated_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.sqlite"
    destination = tmp_path / "wdi.sqlite"
    with closing(_create_database(source, old_schema=True)) as conn, conn:
        _add_machine(conn, "old-machine")
        conn.execute("INSERT INTO projects VALUES (1, 'shared'), (2, 'legacy-only')")
        conn.execute("INSERT INTO project_paths VALUES (2, 'old-machine', 'old/path')")
        _add_session(conn, "already-file", "old-machine", 1, "stale file")
        _add_session(conn, "already-legacy", "old-machine", 1, "stale legacy")
        _add_session(conn, "new-shared", "old-machine", 1, "shared project")
        _add_session(conn, "new-project", "old-machine", 2, "imported")
        _add_analysis(conn, "already-file", "old-machine", "paid file")
        _add_analysis(conn, "new-project", "old-machine", "paid legacy")
        _add_coach(conn, "old-filter", "paid coach")
    with closing(_create_database(destination)) as conn, conn:
        _add_machine(conn, "new-machine")
        conn.execute("INSERT INTO projects VALUES (2, 'unrelated'), (7, 'shared')")
        _add_session(conn, "already-file", "new-machine", 7, "fresh file")
        _add_session(conn, "already-legacy", "new-machine", 7, "kept legacy")
        conn.execute("UPDATE sessions SET origin = 'legacy' WHERE session_id = 'already-legacy'")
        _add_analysis(conn, "already-legacy", "new-machine", "existing analysis")
        _add_coach(conn, "new-filter", "existing coach")
    source_bytes = source.read_bytes()

    for _ in range(2):
        _run_script("import_legacy_db.py", destination, "--from", str(source))

    assert source.read_bytes() == source_bytes
    with closing(connect(destination)) as conn:
        sessions = conn.execute(
            """SELECT session_id, origin, title, canonical_name, sessions.project_id
               FROM sessions JOIN projects USING (project_id) ORDER BY session_id"""
        ).fetchall()
        assert [tuple(row)[:4] for row in sessions] == [
            ("already-file", "file", "fresh file", "shared"),
            ("already-legacy", "legacy", "kept legacy", "shared"),
            ("new-project", "legacy", "imported", "legacy-only"),
            ("new-shared", "legacy", "shared project", "shared"),
        ]
        assert sessions[2]["project_id"] != 2
        assert sessions[3]["project_id"] == 7
        assert tuple(conn.execute("SELECT * FROM project_paths").fetchone()) == (
            sessions[2]["project_id"],
            "old-machine",
            "old/path",
        )
        _assert_transcripts(
            conn,
            {
                "already-file": "fresh file",
                "already-legacy": "kept legacy",
                "new-project": "imported",
                "new-shared": "shared project",
            },
        )
        assert list(
            map(
                tuple,
                conn.execute("SELECT session_id, result FROM llm_analyses ORDER BY session_id"),
            )
        ) == [
            ("already-file", "paid file"),
            ("already-legacy", "existing analysis"),
            ("new-project", "paid legacy"),
        ]
        assert list(
            map(
                tuple,
                conn.execute("SELECT filter_hash, result FROM coach_reports ORDER BY filter_hash"),
            )
        ) == [
            ("new-filter", "existing coach"),
            ("old-filter", "paid coach"),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rebuild_database_keeps_paid_data_and_unrecoverable_transcripts(tmp_path: Path) -> None:
    db = tmp_path / "wdi.sqlite"
    with closing(_create_database(db)) as conn, conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute("ALTER TABLE sessions ADD COLUMN obsolete_note TEXT")
        _add_machine(conn, "machine")
        conn.execute("INSERT INTO projects VALUES (5, 'project')")
        _add_session(conn, "legacy", "machine", 5, "unrecoverable transcript")
        _add_session(conn, "paid-file", "machine", 5, "recoverable transcript")
        _add_session(conn, "unpaid-file", "machine", 5, "disposable transcript")
        conn.execute("UPDATE sessions SET origin = 'legacy' WHERE session_id = 'legacy'")
        _add_analysis(conn, "legacy", "machine", "paid legacy")
        _add_analysis(conn, "paid-file", "machine", "paid file")
        _add_coach(conn, "filter", "paid coach")
        conn.execute(
            "INSERT INTO import_state VALUES ('machine', 'missing.jsonl', 1, 2, 3, 'date')"
        )

    output = _run_script("rebuild_db.py", db)

    assert "wdi import --full" in output
    [backup] = tmp_path.glob("wdi.sqlite.bak-*")
    with closing(_connect_fixture(backup)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        _assert_transcripts(
            conn,
            {
                "legacy": "unrecoverable transcript",
                "paid-file": "recoverable transcript",
                "unpaid-file": "disposable transcript",
            },
        )
    with closing(connect(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert list(
            map(tuple, conn.execute("SELECT session_id, origin FROM sessions ORDER BY session_id"))
        ) == [
            ("legacy", "legacy"),
            ("paid-file", "file"),
        ]
        assert tuple(
            conn.execute(
                """SELECT machine_id, canonical_name, source_path, title, input_tokens
                   FROM sessions JOIN projects USING (project_id)
                   WHERE session_id = 'legacy'"""
            ).fetchone()
        ) == ("machine", "project", "missing.jsonl", "unrecoverable transcript", 41)
        _assert_transcripts(conn, {"legacy": "unrecoverable transcript"})
        assert list(
            map(
                tuple,
                conn.execute("SELECT session_id, result FROM llm_analyses ORDER BY session_id"),
            )
        ) == [
            ("legacy", "paid legacy"),
            ("paid-file", "paid file"),
        ]
        assert tuple(conn.execute("SELECT filter_hash, result FROM coach_reports").fetchone()) == (
            "filter",
            "paid coach",
        )
        assert conn.execute("SELECT * FROM import_state").fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("conflicting_table", ["llm_analyses", "coach_reports"])
def test_legacy_import_rejects_conflicting_paid_records(
    tmp_path: Path, conflicting_table: str
) -> None:
    source = tmp_path / "old.sqlite"
    destination = tmp_path / "wdi.sqlite"
    with closing(_create_database(source, old_schema=True)) as conn, conn:
        _add_machine(conn, "machine")
        conn.execute("INSERT INTO projects VALUES (1, 'project')")
        _add_session(conn, "shared", "machine", 1, "source session")
        _add_session(conn, "legacy", "machine", 1, "unrecoverable session")
        if conflicting_table == "llm_analyses":
            _add_analysis(conn, "shared", "machine", "source analysis")
        else:
            _add_coach(conn, "filter", "source coach")
    with closing(_create_database(destination)) as conn, conn:
        _add_machine(conn, "machine")
        conn.execute("INSERT INTO projects VALUES (1, 'project')")
        _add_session(conn, "shared", "machine", 1, "destination session")
        _add_analysis(conn, "shared", "machine", "destination analysis")
        _add_coach(conn, "filter", "destination coach")

    result = subprocess.run(
        [
            sys.executable,
            str(_INSIGHTS / "scripts" / "import_legacy_db.py"),
            "--db",
            str(destination),
            "--from",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert f"Conflicting records in {conflicting_table}" in result.stderr
    with closing(connect(destination)) as conn:
        assert [row[0] for row in conn.execute("SELECT session_id FROM sessions")] == ["shared"]
        assert conn.execute("SELECT result FROM llm_analyses").fetchone()[0] == (
            "destination analysis"
        )
        assert conn.execute("SELECT result FROM coach_reports").fetchone()[0] == "destination coach"
