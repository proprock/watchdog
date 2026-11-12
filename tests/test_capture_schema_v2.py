import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_schema_v2.py"


def _migrate(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *(str(path) for path in paths)],
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
            CREATE TABLE side_files (
                provider TEXT NOT NULL,
                session_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                digest TEXT NOT NULL,
                raw_bytes INTEGER NOT NULL,
                codec TEXT NOT NULL,
                data BLOB NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (provider, session_id, rel_path, digest)
            );
            INSERT INTO side_files VALUES (
                'codex', 'session', 'notes.txt', 'digest', 3, 'raw', X'00FF0A', 'stamp'
            );
            """)
    return path


def test_upgrade_preserves_existing_capture(v1_store: Path) -> None:
    result = _migrate(v1_store)

    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("SELECT * FROM side_files").fetchall() == [
            ("codex", "session", "notes.txt", "digest", 3, "raw", b"\x00\xff\n", "stamp")
        ]
        assert db.execute("SELECT * FROM blobs").fetchall() == []
        assert db.execute("SELECT * FROM session_env").fetchall() == []
    assert not v1_store.with_name("store.json").exists()


def test_v2_retry_updates_stale_manifest(v1_store: Path) -> None:
    manifest_path = v1_store.with_name("store.json")
    original = {"capture_schema": 1, "events_schema": 9, "written_at": "2000-01-01T00:00:00+00:00"}
    manifest_path.write_text(json.dumps(original), encoding="utf-8")

    first = _migrate(v1_store)
    assert first.returncode == 0, first.stderr
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["capture_schema"] == 2
    assert updated["events_schema"] == 9
    assert datetime.fromisoformat(updated["written_at"]).tzinfo == UTC
    assert updated["written_at"] > original["written_at"]
    with closing(sqlite3.connect(v1_store)) as db, db:
        db.execute("INSERT INTO blobs VALUES ('new', 1, 'raw', X'10')")
    manifest_path.write_text(json.dumps(original), encoding="utf-8")

    second = _migrate(v1_store)
    assert second.returncode == 0, second.stderr
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["capture_schema"] == 2
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("SELECT data FROM blobs").fetchone() == (b"\x10",)


@pytest.mark.parametrize("version", [0, 3])
def test_rejects_unsupported_schema(v1_store: Path, version: int) -> None:
    with closing(sqlite3.connect(v1_store)) as db:
        db.execute(f"PRAGMA user_version = {version}")

    result = _migrate(v1_store)

    assert result.returncode != 0
    assert f"unsupported capture schema {version}" in result.stderr
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (version,)


def test_rejects_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "capture.sqlite3"

    result = _migrate(missing)

    assert result.returncode != 0
    assert "capture database does not exist" in result.stderr
    assert not missing.exists()


def test_schema_change_rolls_back_on_ddl_failure(v1_store: Path) -> None:
    with closing(sqlite3.connect(v1_store)) as db:
        db.execute("CREATE TABLE session_env (existing TEXT)")

    result = _migrate(v1_store)

    assert result.returncode != 0
    assert "table session_env already exists" in result.stderr
    with closing(sqlite3.connect(v1_store)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("SELECT name FROM sqlite_schema WHERE name = 'blobs'").fetchone() is None
