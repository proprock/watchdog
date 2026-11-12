import sqlite3
from pathlib import Path

import pytest

from agent_watchdog.storage import StorageError

_NOW = "2026-11-12T12:00:00+00:00"


def _add_stream(
    db: sqlite3.Connection,
    session_id: str = "session",
    *,
    provider: str = "codex",
    hook_session_id: str | None = None,
    state: str = "open",
) -> int:
    row = db.execute(
        """
        INSERT INTO streams (
            provider, session_id, stream_id, generation, hook_session_id,
            source_path, source_kind, head_len, head_digest, byte_offset,
            record_count, raw_bytes, first_seen, last_growth, last_read, state
        ) VALUES (?, ?, 'main', 0, ?, 'synthetic.jsonl', 'hook', 0, '', 0,
                  0, 0, ?, ?, ?, ?) RETURNING stream_pk
        """,
        (provider, session_id, hook_session_id, _NOW, _NOW, _NOW, state),
    ).fetchone()
    return row[0]


def _append(db: sqlite3.Connection, stream_pk: int, lines: list[bytes]) -> None:
    db.executemany(
        "INSERT INTO tail (stream_pk, record_seq, line) VALUES (?, ?, ?)",
        ((stream_pk, seq, line) for seq, line in enumerate(lines)),
    )
    db.execute(
        "UPDATE streams SET record_count = ?, raw_bytes = ? WHERE stream_pk = ?",
        (len(lines), sum(len(line) + 1 for line in lines), stream_pk),
    )


def test_create_and_reopen_capture_store(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CAPTURE_FILENAME, CaptureStore

    path = tmp_path / CAPTURE_FILENAME
    assert path.name == "capture.sqlite3"
    with CaptureStore(path).connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        stream_pk = _add_stream(db)

    with CaptureStore(path).connect() as db:
        row = db.execute("SELECT * FROM streams").fetchone()
        assert row["stream_pk"] == stream_pk
        assert row["session_id"] == "session"


@pytest.mark.parametrize("version", [0, 7])
def test_reject_unsupported_capture_schema(tmp_path: Path, version: int) -> None:
    from agent_watchdog.capture_store import CaptureStore

    path = tmp_path / "capture.sqlite3"
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE existing (name TEXT)")
        db.execute(f"PRAGMA user_version = {version}")
    db.close()

    with (
        pytest.raises(StorageError, match=rf"version {version}.*expected 1.*convert_capture_store"),
        CaptureStore(path).connect(),
    ):
        pytest.fail("Opening an unsupported capture schema must fail")


def test_failed_transaction_rolls_back(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore

    store = CaptureStore(tmp_path / "capture.sqlite3")
    with pytest.raises(RuntimeError, match="interrupted"), store.connect() as db:
        _add_stream(db)
        raise RuntimeError("interrupted")

    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 0


def test_seal_and_reopen_preserves_chunk_and_tail_order(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore, iter_lines, seal_tail

    store = CaptureStore(tmp_path / "capture.sqlite3")
    lines = [b"\xff\r", b"", b"123", b"4567", b"z"]
    with store.connect() as db:
        stream_pk = _add_stream(db)
        _append(db, stream_pk, lines)
        seal_tail(db, stream_pk, target_bytes=5)
        seal_tail(db, stream_pk, target_bytes=5)

    with store.connect() as db:
        assert list(iter_lines(db, stream_pk)) == lines
        chunks = db.execute(
            "SELECT chunk_seq, first_record, record_count, raw_bytes, codec "
            "FROM chunks ORDER BY chunk_seq"
        ).fetchall()
        assert [tuple(row) for row in chunks] == [
            (0, 0, 2, 4, "zstd"),
            (1, 2, 1, 4, "zstd"),
        ]
        assert db.execute("SELECT COUNT(*) FROM tail").fetchone()[0] == 2


def test_idle_seal_flushes_tail_smaller_than_target(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore, iter_lines, seal_tail

    store = CaptureStore(tmp_path / "capture.sqlite3")
    with store.connect() as db:
        stream_pk = _add_stream(db)
        _append(db, stream_pk, [b"small"])
        seal_tail(db, stream_pk, target_bytes=8)
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0

        seal_tail(db, stream_pk, target_bytes=8, idle=True)
        assert db.execute("SELECT COUNT(*) FROM tail").fetchone()[0] == 0
        assert list(iter_lines(db, stream_pk)) == [b"small"]


def test_seal_stores_large_record_whole(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore, iter_lines, seal_tail

    store = CaptureStore(tmp_path / "capture.sqlite3")
    with store.connect() as db:
        stream_pk = _add_stream(db)
        _append(db, stream_pk, [b"oversize", b"tail"])
        seal_tail(db, stream_pk, target_bytes=4)
        assert db.execute("SELECT record_count FROM chunks").fetchone()[0] == 1
        assert list(iter_lines(db, stream_pk)) == [b"oversize", b"tail"]


def test_missing_capture_store_stays_absent_on_maintenance(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore

    path = tmp_path / "capture.sqlite3"
    store = CaptureStore(path)
    assert store.stats()["streams"] == 0
    assert store.purge("codex", "session") == 0
    assert not path.exists()


def test_purge_removes_capture_rows_for_session_and_hook_session(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore, seal_tail

    store = CaptureStore(tmp_path / "capture.sqlite3")
    with store.connect() as db:
        main = _add_stream(db)
        child = _add_stream(db, "child", hook_session_id="session")
        other_provider = _add_stream(db, provider="claude")
        _append(db, main, [b"main"])
        seal_tail(db, main, target_bytes=4)
        _append(db, child, [b"child"])
        _append(db, other_provider, [b"other"])
        db.execute(
            "INSERT INTO side_files VALUES ('codex', 'session', 'aux.json', 'digest', "
            "5, 'zstd', x'00', ?)",
            (_NOW,),
        )

    assert store.purge("codex", "session") == 5
    with store.connect() as db:
        assert db.execute("SELECT stream_pk FROM streams").fetchone()[0] == other_provider
        assert db.execute("SELECT stream_pk FROM tail").fetchone()[0] == other_provider
        assert db.execute("SELECT * FROM chunks").fetchone() is None
        assert db.execute("SELECT * FROM side_files").fetchone() is None


def test_stats_counts_stream_states_and_payload_sizes(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore, seal_tail

    store = CaptureStore(tmp_path / "capture.sqlite3")
    with store.connect() as db:
        main = _add_stream(db)
        missing = _add_stream(db, "missing", state="missing")
        _append(db, main, [b"123456789", b"tail"])
        seal_tail(db, main, target_bytes=10)
        db.execute(
            "UPDATE streams SET skipped_lines = 2, last_error = 'read failed' WHERE stream_pk = ?",
            (missing,),
        )
        db.execute(
            "INSERT INTO side_files VALUES ('codex', 'session', 'aux.json', 'digest', "
            "8, 'zstd', x'1234', ?)",
            (_NOW,),
        )
        chunk_size = db.execute("SELECT length(data) FROM chunks").fetchone()[0]

    assert store.stats() == {
        "streams": 2,
        "open": 1,
        "missing": 1,
        "raw_bytes": 23,
        "stored_bytes": chunk_size + 6,
        "skipped_lines": 2,
        "last_errors": ["read failed"],
    }
