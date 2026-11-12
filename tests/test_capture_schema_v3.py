import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _migrate(path: Path, version: int = 3) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / f"capture_schema_v{version}.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def v1_store(tmp_path: Path) -> Path:
    path = tmp_path / "capture.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            PRAGMA user_version = 1;
            CREATE TABLE side_files (data BLOB NOT NULL);
            INSERT INTO side_files VALUES (X'00FF0A');
            """)
    return path


@pytest.fixture
def v2_store(v1_store: Path) -> Path:
    result = _migrate(v1_store, version=2)
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(v1_store)) as db, db:
        db.execute("INSERT INTO blobs VALUES ('digest', 3, 'raw', X'00FF0A')")
        db.execute("""
            INSERT INTO session_env (
                provider, session_id, captured_at, head, harness, files, watchdog,
                initial_diff_digest
            ) VALUES ('codex', 'session', 'stamp', 'base', '{}', '[]', '{}', 'digest')
            """)
    return v1_store


def test_v1_upgrades_directly_to_v3(v1_store: Path) -> None:
    result = _migrate(v1_store)

    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert db.execute("SELECT data FROM side_files").fetchone() == (b"\x00\xff\n",)
        assert db.execute("SELECT * FROM blobs").fetchall() == []
        assert db.execute("SELECT * FROM session_env").fetchall() == []
        assert db.execute("SELECT * FROM checkout_states").fetchall() == []
    assert not v1_store.with_name("store.json").exists()


def test_v2_upgrade_preserves_session_environment_and_blob(v2_store: Path) -> None:
    result = _migrate(v2_store)

    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(v2_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert db.execute("SELECT * FROM blobs").fetchall() == [("digest", 3, "raw", b"\x00\xff\n")]
        assert db.execute("""
            SELECT provider, session_id, captured_at, head, harness, files, watchdog,
                   initial_diff_digest, initial_files_changed, initial_insertions,
                   initial_deletions, initial_untracked
            FROM session_env
            """).fetchall() == [
            (
                "codex",
                "session",
                "stamp",
                "base",
                "{}",
                "[]",
                "{}",
                "digest",
                None,
                None,
                None,
                None,
            )
        ]


@pytest.mark.parametrize("stale_version", [1, 2])
def test_v3_retry_updates_stale_manifest(v1_store: Path, stale_version: int) -> None:
    first = _migrate(v1_store)
    assert first.returncode == 0, first.stderr
    manifest_path = v1_store.with_name("store.json")
    original = {
        "capture_schema": stale_version,
        "events_schema": 9,
        "written_at": "2000-01-01T00:00:00+00:00",
    }
    manifest_path.write_text(json.dumps(original), encoding="utf-8")

    retry = _migrate(v1_store)

    assert retry.returncode == 0, retry.stderr
    assert "already at schema 3" in retry.stdout
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["capture_schema"] == 3
    assert updated["events_schema"] == 9
    assert datetime.fromisoformat(updated["written_at"]).tzinfo == UTC
    assert updated["written_at"] > original["written_at"]


def test_v1_upgrade_rolls_back_on_v3_ddl_failure(v1_store: Path) -> None:
    with closing(sqlite3.connect(v1_store)) as db:
        db.execute("CREATE TABLE checkout_states (existing TEXT)")
    manifest_path = v1_store.with_name("store.json")
    original = '{"capture_schema": 1}'
    manifest_path.write_text(original, encoding="utf-8")

    result = _migrate(v1_store)

    assert result.returncode != 0
    assert "table checkout_states already exists" in result.stderr
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('blobs', 'session_env')"
            ).fetchall()
            == []
        )
    assert manifest_path.read_text(encoding="utf-8") == original
