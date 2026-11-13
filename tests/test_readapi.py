import json
import sqlite3
from compression import zstd
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_watchdog import capture_store, config
from agent_watchdog.storage import Store

_NOW = "2026-11-12T12:00:00+00:00"


def _manifest(data_dir: Path, name: str = "sample") -> Path:
    project = config.Project(id=uuid4(), root=data_dir.parent / name)
    project_dir = data_dir / "projects" / str(project.id)
    project_dir.mkdir(parents=True)
    capture_store.write_manifest(project_dir, machine_id=uuid4(), project=project)
    return project_dir


def _replace_manifest_field(project_dir: Path, field: str, value: int) -> None:
    path = project_dir / "store.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _database(project_dir: Path, kind: str) -> None:
    if kind == "events":
        with Store(project_dir, UUID(project_dir.name)):
            pass
    else:
        with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect():
            pass


def _stream(
    db: sqlite3.Connection,
    lines: list[bytes],
    *,
    generation: int = 0,
    stream_id: str = "main",
    session_id: str = "sample-session",
) -> int:
    row = db.execute(
        """
        INSERT INTO streams (
            provider, session_id, stream_id, generation, source_path, source_kind, cwd,
            head_len, head_digest, byte_offset, record_count, raw_bytes,
            first_seen, last_growth, last_read, state
        ) VALUES ('claude', ?, ?, ?, 'sample.jsonl', 'hook', 'sample-cwd',
                  0, '', 0, ?, ?, ?, ?, ?, 'open') RETURNING stream_pk
        """,
        (
            session_id,
            stream_id,
            generation,
            len(lines),
            sum(len(line) + 1 for line in lines),
            _NOW,
            _NOW,
            _NOW,
        ),
    ).fetchone()
    assert row is not None
    stream_pk = row[0]
    db.executemany(
        "INSERT INTO tail (stream_pk, record_seq, line) VALUES (?, ?, ?)",
        ((stream_pk, seq, line) for seq, line in enumerate(lines)),
    )
    return stream_pk


def test_stores_skips_missing_manifests_and_flags_future_contract(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    current = _manifest(tmp_path, "current")
    future = _manifest(tmp_path, "future")
    (tmp_path / "projects" / str(uuid4())).mkdir()
    _replace_manifest_field(future, "contract", 2)

    found = {str(info.project_id): info for info in readapi.stores(tmp_path)}
    assert set(found) == {current.name, future.name}
    assert found[current.name].supported is True
    assert found[current.name].contract == 1
    assert found[current.name].name == "current"
    assert found[current.name].root == str(tmp_path.parent / "current")
    assert found[future.name].supported is False
    assert found[future.name].contract == 2


@pytest.mark.parametrize("reader", ["events", "event_facts"])
def test_event_readers_page_by_parent_rowid_and_release_snapshot(tmp_path, monkeypatch, reader):
    from agent_watchdog import readapi
    from agent_watchdog.events import Envelope

    project_dir = _manifest(tmp_path)
    with Store(project_dir, UUID(project_dir.name)) as store:
        for number in range(3):
            store.put(
                Envelope(
                    provider="claude",
                    project_id=UUID(project_dir.name),
                    session_id="sample-session",
                    kind="tool.finish",
                    source="hook",
                    payload={
                        "claude": {"tool_name": "Read", "metadata": {"tool_duration_ms": number}}
                    },
                )
            )
    (info,) = readapi.stores(tmp_path)
    monkeypatch.setattr(readapi, "_PAGE_SIZE", 1)
    iterator = getattr(readapi, reader)(info, after_rowid=1)
    second = next(iterator)
    assert second["event_rowid"] == 2
    assert second["session_id"] == "sample-session"
    with sqlite3.connect(project_dir / "events.sqlite3", timeout=0.1) as db:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        third_id = db.execute("SELECT event_id FROM events WHERE rowid = 3").fetchone()[0]
        if reader == "event_facts":
            db.execute("UPDATE event_facts SET rowid = 99 WHERE event_id = ?", (third_id,))
    third = next(iterator)
    assert third["event_rowid"] == 3
    assert list(iterator) == []
    if reader == "event_facts":
        assert set(third) == {"event_rowid", *readapi.FACT_COLUMNS}
        assert third["tool_name"] == "Read"


@pytest.mark.parametrize("reader", ["events", "event_facts"])
def test_event_readers_handle_missing_database_and_reject_future_contract(tmp_path, reader):
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    (info,) = readapi.stores(tmp_path)
    assert list(getattr(readapi, reader)(info)) == []
    _replace_manifest_field(project_dir, "contract", 2)
    (future,) = readapi.stores(tmp_path)
    with pytest.raises(ValueError, match="(?i)contract"):
        list(getattr(readapi, reader)(future))


def test_project_without_capture_database_lists_no_transcripts(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    (info,) = readapi.stores(tmp_path)

    assert list(readapi.streams(info)) == []
    assert list(readapi.side_files(info, "claude", "sample-session")) == []
    with pytest.raises(FileNotFoundError), readapi.open_capture(info):
        pytest.fail("Opening a missing database succeeded")
    assert not (project_dir / capture_store.CAPTURE_FILENAME).exists()


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_is_read_only(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with open_database(info) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden (id INTEGER)")


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_refuses_future_contract(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    _replace_manifest_field(project_dir, "contract", 2)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with pytest.raises(ValueError, match="(?i)contract"), open_database(info):
        pytest.fail("A newer contract opened successfully")


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_refuses_manifest_schema_mismatch(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    _replace_manifest_field(project_dir, f"{kind}_schema", 123)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with pytest.raises(ValueError, match="(?i)(version|schema)"), open_database(info):
        pytest.fail("A database with the wrong schema version opened successfully")


def test_open_events_refuses_database_owned_by_another_project(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    with Store(project_dir, uuid4()):
        pass
    (info,) = readapi.stores(tmp_path)

    with pytest.raises(ValueError, match="(?i)project"), readapi.open_events(info):
        pytest.fail("A database owned by another project opened successfully")


def test_streams_yields_latest_generation_per_stream(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        latest = _stream(db, [b"one", b"two"], generation=1)
        _stream(db, [b"earlier"])
        subagent = _stream(db, [b"child"], stream_id="a12f")
    (info,) = readapi.stores(tmp_path)

    streams = {stream.stream_pk: stream for stream in readapi.streams(info)}
    assert set(streams) == {latest, subagent}
    main = streams[latest]
    assert (main.provider, main.session_id, main.stream_id) == ("claude", "sample-session", "main")
    assert (main.generation, main.record_count, main.raw_bytes) == (1, 2, 8)
    assert main.cwd == "sample-cwd"


@pytest.mark.parametrize("start_record", [0, 1, 2, 3, 4, 5, 6])
def test_lines_preserves_exact_bytes_from_sealed_chunks_and_tail(
    tmp_path: Path, start_record: int
) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    expected = [b"\xff\r", b"", b"123", b"4567", b"z"]
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        stream_pk = _stream(db, expected)
        capture_store.seal_tail(db, stream_pk, target_bytes=5)
        capture_store.seal_tail(db, stream_pk, target_bytes=5)
    (info,) = readapi.stores(tmp_path)

    assert (
        list(readapi.lines(info, stream_pk, start_record=start_record)) == expected[start_record:]
    )


@pytest.mark.parametrize("consumed", [1, 2])
def test_suspended_lines_iterator_allows_checkpoint_during_tail_seal(
    tmp_path: Path, consumed: int
) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    path = project_dir / capture_store.CAPTURE_FILENAME
    store = capture_store.CaptureStore(path)
    expected = [b"sealed", b"first tail", b"second tail", b"last tail"]
    with store.connect() as db:
        stream_pk = _stream(db, expected)
        capture_store.seal_tail(db, stream_pk, target_bytes=len(expected[0]) + 1)
    (info,) = readapi.stores(tmp_path)

    with closing(sqlite3.connect(path, timeout=0)) as checkpoint:
        checkpoint.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        records = readapi.lines(info, stream_pk)
        assert [next(records) for _ in range(consumed)] == expected[:consumed]
        with store.connect() as db:
            capture_store.seal_tail(db, stream_pk, idle=True)
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0
        assert tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
        assert list(records) == expected[consumed:]


def test_suspended_lines_rejects_a_reused_stream_key(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    store = capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME)
    with store.connect() as db:
        stream_pk = _stream(db, [b"first", b"original"])
        capture_store.seal_tail(db, stream_pk, target_bytes=6)
    (info,) = readapi.stores(tmp_path)
    records = readapi.lines(info, stream_pk)
    assert next(records) == b"first"

    store.purge("claude", "sample-session")
    with store.connect() as db:
        assert _stream(db, [b"other first", b"other tail"], session_id="replacement") == stream_pk

    with pytest.raises(ValueError, match="(?i)stream.*changed"):
        next(records)


def test_side_files_list_metadata_and_read_original_bytes(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    path = "tool-results/output"
    expected = [b"initial\r\n", b"replacement\x00\xff"]
    digests = [sha256(data).hexdigest() for data in expected]
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        db.executemany(
            """
            INSERT INTO side_files (
                provider, session_id, rel_path, digest, raw_bytes, codec, data, captured_at
            ) VALUES (?, ?, ?, ?, ?, 'zstd', ?, ?)
            """,
            [
                ("claude", "sample-session", path, digest, len(data), zstd.compress(data), _NOW)
                for digest, data in zip(digests, expected, strict=True)
            ]
            + [("claude", "other-session", "other", "other", 0, zstd.compress(b""), _NOW)],
        )
    (info,) = readapi.stores(tmp_path)

    files = list(readapi.side_files(info, "claude", "sample-session"))
    assert {(file.rel_path, file.digest, file.raw_bytes, file.captured_at) for file in files} == {
        (path, digest, len(data), _NOW) for digest, data in zip(digests, expected, strict=True)
    }
    assert [
        readapi.side_file_bytes(info, "claude", "sample-session", path, digest)
        for digest in digests
    ] == expected
