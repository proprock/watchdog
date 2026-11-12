from __future__ import annotations

import json
import sqlite3
from compression import zstd
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_watchdog.storage import StorageError

if TYPE_CHECKING:
    from agent_watchdog.capture_store import SessionEnvironment

_NOW = "2026-11-12T12:00:00+00:00"
_LATER = "2026-11-12T12:00:01+00:00"


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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        stream_pk = _add_stream(db)

    with CaptureStore(path).connect() as db:
        row = db.execute("SELECT * FROM streams").fetchone()
        assert row["stream_pk"] == stream_pk
        assert row["session_id"] == "session"


@pytest.mark.parametrize("version", [0, 1, 2, 7])
def test_reject_unsupported_capture_schema(tmp_path: Path, version: int) -> None:
    from agent_watchdog.capture_store import CaptureStore

    path = tmp_path / "capture.sqlite3"
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE existing (name TEXT)")
        db.execute(f"PRAGMA user_version = {version}")
    db.close()

    with (
        pytest.raises(StorageError, match=rf"version {version}.*expected 3.*docs/storage.md"),
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


def _session_environment(file_digest: str) -> SessionEnvironment:
    from agent_watchdog.capture_store import SessionEnvironment

    return SessionEnvironment(
        provider="codex",
        session_id="session",
        captured_at=_NOW,
        cwd="checkout/subdir",
        checkout_id="checkout-id",
        head="a" * 40,
        branch="feature/capture",
        harness={"model": "test-model"},
        files=[{"path": "AGENTS.md", "role": "instructions", "digest": file_digest, "error": None}],
        watchdog={"global_kill_switch": True},
    )


@pytest.mark.parametrize("capture_content", [True, False])
def test_record_session_environment(tmp_path: Path, capture_content: bool) -> None:
    from agent_watchdog.capture_store import CaptureStore

    store = CaptureStore(tmp_path / "capture.sqlite3")
    instructions = b"Synthetic instructions\n"
    patch = b"diff --git a/file b/file\n"
    file_digest = sha256(instructions).hexdigest()
    patch_digest = sha256(patch).hexdigest()
    snapshot = replace(_session_environment(file_digest), initial_diff_digest=patch_digest)
    payloads = {file_digest: instructions, patch_digest: patch}

    assert store.record_session_env(snapshot, payloads, capture_content=capture_content)

    with store.connect() as db:
        row = db.execute("SELECT * FROM session_env").fetchone()
        for field in (
            "provider",
            "session_id",
            "captured_at",
            "cwd",
            "checkout_id",
            "head",
            "branch",
            "initial_diff_digest",
            "initial_diff_error",
        ):
            assert row[field] == getattr(snapshot, field)
        for field in ("harness", "files", "watchdog"):
            assert json.loads(row[field]) == getattr(snapshot, field)
        blobs = db.execute("SELECT digest, raw_bytes, codec, data FROM blobs").fetchall()

    if capture_content:
        assert {row["digest"] for row in blobs} == payloads.keys()
        for digest, raw_bytes, codec, data in blobs:
            assert (raw_bytes, codec, zstd.decompress(data)) == (
                len(payloads[digest]),
                "zstd",
                payloads[digest],
            )
    else:
        assert blobs == []


def test_equal_environment_keeps_only_one_snapshot(tmp_path: Path) -> None:
    from agent_watchdog.capture_store import CaptureStore

    store = CaptureStore(tmp_path / "capture.sqlite3")
    instructions = b"Synthetic instructions\n"
    digest = sha256(instructions).hexdigest()
    snapshot = _session_environment(digest)
    payloads = {digest: instructions}

    assert store.record_session_env(snapshot, payloads, capture_content=True)
    assert not store.record_session_env(
        replace(snapshot, captured_at=_LATER), payloads, capture_content=True
    )

    with store.connect() as db:
        assert [row[0] for row in db.execute("SELECT captured_at FROM session_env")] == [_NOW]
        assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


def test_snapshot_respects_quota_and_retains_digests(tmp_path: Path, monkeypatch) -> None:
    from agent_watchdog.capture_store import CaptureStore

    instructions = b"instructions"
    digest = sha256(instructions).hexdigest()
    snapshot = _session_environment(digest)
    monkeypatch.setattr("agent_watchdog.resources.available", lambda *_: 72 * 1024)
    store = CaptureStore(tmp_path / "capture.sqlite3")
    assert store.record_session_env(snapshot, {digest: instructions}, capture_content=True)
    with store.connect(write=False) as db:
        (files,) = db.execute("SELECT files FROM session_env").fetchone()
        assert json.loads(files)[0]["digest"] == digest
        assert json.loads(files)[0]["error"] == "quota"
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


def test_purge_session_environment_retains_blobs_referenced_by_other_sessions(
    tmp_path: Path,
) -> None:
    from agent_watchdog.capture_store import CaptureStore

    store = CaptureStore(tmp_path / "capture.sqlite3")
    instructions = b"Shared payload\n"
    patch = b"Unshared patch\n"
    shared_digest = sha256(instructions).hexdigest()
    patch_digest = sha256(patch).hexdigest()
    snapshot = replace(_session_environment(shared_digest), initial_diff_digest=patch_digest)
    other = replace(snapshot, provider="claude", files=[], initial_diff_digest=shared_digest)

    assert store.record_session_env(
        snapshot, {shared_digest: instructions, patch_digest: patch}, capture_content=True
    )
    assert store.record_session_env(other, {shared_digest: instructions}, capture_content=True)
    store.purge("codex", snapshot.session_id)

    with store.connect() as db:
        assert [row[0] for row in db.execute("SELECT provider FROM session_env")] == ["claude"]
        assert [row[0] for row in db.execute("SELECT digest FROM blobs")] == [shared_digest]

    store.purge("claude", other.session_id)

    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_env").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
