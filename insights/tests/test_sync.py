"""Tests for snapshot export/import merge."""

from contextlib import closing
from pathlib import Path

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.sync.snapshot import export_snapshot, import_snapshots


def _make_db(path: Path, machine_id: str, *, project: str, session_id: str) -> None:
    conn = connect(path)
    repo = Repository(conn)
    repo.ensure_machine(machine_id, hostname=f"host-{machine_id}")
    pid = repo.get_or_create_project(project)
    repo.link_project_path(pid, machine_id, f"C:/{machine_id}/{project}")
    conn.execute(
        """
        INSERT INTO sessions (session_id, machine_id, project_id, agent, model, started_at,
                              input_tokens, output_tokens, tool_call_count)
        VALUES (?, ?, ?, 'claude', 'claude-opus-4-8', '2026-06-01T10:00:00Z', 100, 50, 1)
        """,
        (session_id, machine_id, pid),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, machine_id, stream_id, idx, role, content_preview)
        VALUES (?, ?, 'main', 0, 'user', 'hello')
        """,
        (session_id, machine_id),
    )
    conn.execute(
        """
        INSERT INTO tool_calls (session_id, machine_id, stream_id, idx, tool_name, category)
        VALUES (?, ?, 'main', 0, 'Read', 'read')
        """,
        (session_id, machine_id),
    )
    conn.execute(
        """
        INSERT INTO context_events
            (session_id, machine_id, stream_id, idx, event_type, category, byte_count)
        VALUES (?, ?, 'main', 0, 'message', 'user_prompt', 5)
        """,
        (session_id, machine_id),
    )
    repo.upsert_llm_analysis(
        session_id,
        machine_id,
        analysis_type="prompt_quality",
        prompt_version="v1",
        model="haiku",
        score=80.0,
        result={"summary": "ok"},
    )
    conn.commit()
    conn.close()


def test_export_creates_named_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    _make_db(db, "machineA", project="demo", session_id="sA")
    out_dir = tmp_path / "sync"

    path = export_snapshot(db, out_dir, "machineA")

    assert path.name == "wdi-machineA.sqlite"
    assert path.is_file()


def test_import_merges_foreign_sessions(tmp_path: Path) -> None:
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"
    _make_db(db_a, "machineA", project="demo", session_id="sA")
    _make_db(db_b, "machineB", project="demo", session_id="sB")

    sync = tmp_path / "sync"
    export_snapshot(db_b, sync, "machineB")

    stats = import_snapshots(db_a, sync, "machineA")

    assert stats.snapshots == 1
    assert stats.sessions == 1
    assert stats.messages == 1
    assert stats.context_events == 1
    assert stats.llm_analyses == 1

    conn = connect(db_a)
    try:
        sessions = {r["session_id"] for r in conn.execute("SELECT session_id FROM sessions")}
        assert sessions == {"sA", "sB"}
        # Same project name from both machines collapses to one project row.
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        # Foreign session's project_id points at the local project.
        row = conn.execute(
            "SELECT p.canonical_name FROM sessions s JOIN projects p "
            "ON p.project_id = s.project_id WHERE s.session_id = 'sB'"
        ).fetchone()
        assert row["canonical_name"] == "demo"
    finally:
        conn.close()


def test_import_is_idempotent(tmp_path: Path) -> None:
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"
    _make_db(db_a, "machineA", project="demo", session_id="sA")
    _make_db(db_b, "machineB", project="other", session_id="sB")
    sync = tmp_path / "sync"
    export_snapshot(db_b, sync, "machineB")

    first = import_snapshots(db_a, sync, "machineA")
    second = import_snapshots(db_a, sync, "machineA")

    assert first.sessions == 1
    assert second.sessions == 0  # nothing new on the second merge

    conn = connect(db_a)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM llm_analyses").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.parametrize("origin", ["legacy", "watchdog"])
def test_snapshot_import_preserves_session_origin(tmp_path: Path, origin: str) -> None:
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "destination.sqlite"
    _make_db(source, "machineB", project="demo", session_id="archived")
    with closing(connect(source)) as conn, conn:
        conn.execute("UPDATE sessions SET origin = ?", (origin,))

    sync = tmp_path / "sync"
    export_snapshot(source, sync, "machineB")
    assert import_snapshots(destination, sync, "machineA").sessions == 1
    with closing(connect(destination)) as conn:
        assert conn.execute("SELECT origin FROM sessions").fetchone()[0] == origin
        assert conn.execute("SELECT content_preview FROM messages").fetchone()[0] == "hello"


def test_snapshot_import_leaves_existing_legacy_streams_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "destination.sqlite"
    _make_db(source, "machineB", project="demo", session_id="archived")
    _make_db(destination, "machineA", project="demo", session_id="archived")
    with closing(connect(destination)) as conn, conn:
        conn.execute("UPDATE sessions SET origin = 'legacy'")
        conn.execute("UPDATE messages SET idx = 1, content_preview = 'preserved'")

    sync = tmp_path / "sync"
    export_snapshot(source, sync, "machineB")
    assert import_snapshots(destination, sync, "machineA").snapshots == 1
    with closing(connect(destination)) as conn:
        assert [row[0] for row in conn.execute("SELECT content_preview FROM messages")] == [
            "preserved"
        ]


def test_import_skips_own_snapshot(tmp_path: Path) -> None:
    db_a = tmp_path / "a.sqlite"
    _make_db(db_a, "machineA", project="demo", session_id="sA")
    sync = tmp_path / "sync"
    export_snapshot(db_a, sync, "machineA")

    stats = import_snapshots(db_a, sync, "machineA")

    assert stats.snapshots == 0
    assert stats.sessions == 0


def test_foreign_key_integrity_after_merge(tmp_path: Path) -> None:
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"
    _make_db(db_a, "machineA", project="demo", session_id="sA")
    _make_db(db_b, "machineB", project="demo", session_id="sB")
    sync = tmp_path / "sync"
    export_snapshot(db_b, sync, "machineB")
    import_snapshots(db_a, sync, "machineA")

    conn = connect(db_a)
    try:
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []
    finally:
        conn.close()
