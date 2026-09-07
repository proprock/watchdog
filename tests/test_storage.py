import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Inbox, StorageError, Store, WriterBusy, writer_lock


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range lock initialization")
def test_initial_lock_byte_contention_is_reported_as_busy(tmp_path, monkeypatch):
    class ContendedFile(io.BytesIO):
        def flush(self):
            raise PermissionError("Another owner locked the newly initialized byte")

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: ContendedFile())
    with pytest.raises(WriterBusy):
        with writer_lock(tmp_path / "writer.lock"):
            pytest.fail("Contended lock was acquired")


@pytest.fixture
def event():
    return Envelope(provider="codex", project_id=uuid4(), kind="tool.finish", source="hook")


def test_publish_replay_and_reopen(tmp_path, event):
    inbox = Inbox(tmp_path)
    first = inbox.publish(event)
    second = inbox.publish(event)
    assert first != second
    assert Envelope.model_validate_json(first.read_bytes()) == event
    with Store(tmp_path, event.project_id) as store:
        result = inbox.drain(store)
        assert (result.inserted, result.duplicates) == (1, 1)
        assert store.events() == [event]
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not first.exists() and not second.exists()
    with Store(tmp_path, event.project_id) as reopened:
        assert reopened.events() == [event]


def test_partial_publish_is_not_visible_and_replace_failure_cleans_up(tmp_path, event, monkeypatch):
    inbox = Inbox(tmp_path)
    inbox.directory.mkdir(parents=True)
    partial = inbox.directory / "interrupted.tmp"
    partial.write_text("partial")

    def fail_replace(*args):
        raise OSError("Simulated write failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        inbox.publish(event)
    assert list(inbox.directory.iterdir()) == [partial]
    with Store(tmp_path, event.project_id) as store:
        assert inbox.drain(store).inserted == 0
        assert store.events() == []


@pytest.mark.parametrize("bad", [b"broken JSON", b"[]", b'{"schema_version":2}'])
def test_bad_record_does_not_block_valid_event(tmp_path, event, bad):
    inbox = Inbox(tmp_path)
    good = inbox.publish(event)
    (inbox.directory / "0-bad.json").write_bytes(bad)
    with Store(tmp_path, event.project_id) as store:
        result = inbox.drain(store)
        assert (result.inserted, result.quarantined) == (1, 1)
    assert not good.exists()
    diagnostic = json.loads(next((tmp_path / "quarantine").glob("*.bad")).read_bytes())
    assert diagnostic == {"reason": "rejected", "bytes": len(bad)}


def test_quarantine_is_bounded_and_oversized_input_is_not_retained(tmp_path, event):
    inbox = Inbox(tmp_path, quarantine_files=2, quarantine_bytes=20)
    inbox.publish(event)
    for index in range(5):
        (inbox.directory / f"bad-{index}.json").write_bytes(b"invalid")
    (inbox.directory / "huge.json").write_bytes(b"x" * 21)
    with Store(tmp_path, event.project_id) as store:
        result = inbox.drain(store)
        assert result.inserted == 1
        assert result.discarded >= 4
    files = list((tmp_path / "quarantine").glob("*.bad"))
    assert len(files) <= 2
    assert sum(path.stat().st_size for path in files) <= 20


def test_oversized_publish_is_rejected_before_writing(tmp_path, event):
    with pytest.raises(StorageError):
        Inbox(tmp_path, payload_bytes=10).publish(event)
    assert not (tmp_path / "inbox").exists()


def test_project_mismatch_and_conflicting_uuid_are_quarantined(tmp_path, event):
    inbox = Inbox(tmp_path)
    inbox.publish(event)
    with Store(tmp_path, event.project_id) as store:
        inbox.drain(store)
        changed = event.model_copy(update={"kind": "interrupt"})
        inbox.publish(changed)
        inbox.publish(Envelope(provider="codex", project_id=uuid4(), kind="unknown", source="hook"))
        result = inbox.drain(store)
        assert result.quarantined == 2
        assert store.events() == [event]


def test_failed_transaction_keeps_input_for_replay(tmp_path, event):
    inbox = Inbox(tmp_path)
    incoming = inbox.publish(event)
    with Store(tmp_path, event.project_id) as store:
        store.connection.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'Simulated failure'); END"
        )
        with pytest.raises(sqlite3.DatabaseError):
            inbox.drain(store)
        assert incoming.exists()
        assert store.events() == []
        store.connection.execute("DROP TRIGGER fail_insert")
        assert inbox.drain(store).inserted == 1


@pytest.mark.parametrize("phase,expected_before", [("before", 0), ("after", 1)])
def test_process_crash_replays_without_duplicates(tmp_path, event, phase, expected_before):
    incoming = Inbox(tmp_path).publish(event)
    script = Path(__file__).parent / "storage_crash_probe.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(event.project_id), phase],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 91, result.stderr
    assert incoming.exists()
    with Store(tmp_path, event.project_id) as store:
        assert len(store.events()) == expected_before
        recovered = Inbox(tmp_path).drain(store)
        assert (recovered.inserted, recovered.duplicates) == (1 - expected_before, expected_before)
        assert store.events() == [event]
    assert not incoming.exists()


def test_only_one_writer_and_wrong_project_cannot_reopen(tmp_path, event):
    with Store(tmp_path, event.project_id):
        with pytest.raises(WriterBusy):
            with Store(tmp_path, event.project_id):
                pytest.fail("Second writer acquired the store")
    with pytest.raises(StorageError):
        with Store(tmp_path, uuid4()):
            pytest.fail("Wrong project opened store")
    with Store(tmp_path, event.project_id) as store:
        assert store.events() == []


@pytest.mark.parametrize("version", [5, 99])
def test_newer_database_is_not_changed(tmp_path, event, version):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future(data TEXT)")
        db.execute("INSERT INTO future VALUES ('preserve')")
        db.execute(f"PRAGMA user_version={version}")
    original = path.read_bytes()
    with pytest.raises(StorageError):
        with Store(tmp_path, event.project_id):
            pytest.fail("Newer schema opened")
    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()


def test_unversioned_unrelated_database_is_not_adopted(tmp_path, event):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(data TEXT)")
    original = path.read_bytes()
    with pytest.raises(StorageError):
        with Store(tmp_path, event.project_id):
            pytest.fail("Unrelated schema adopted")
    assert path.read_bytes() == original


def test_artifacts_are_content_addressed_and_transactionally_referenced(tmp_path, event):
    content = b"sanitized tool output"
    with Store(tmp_path, event.project_id) as store:
        assert store.put(event, artifacts={"stdout": content}) is True
        assert store.put(event, artifacts={"stdout": content}) is False
        other = event.model_copy(update={"event_id": uuid4()})
        store.put(other, artifacts={"stderr": content})
        assert store.read_artifact(event.event_id, "stdout") == content
        assert len(list((tmp_path / "artifacts").glob("*.bin"))) == 1
        with pytest.raises(StorageError):
            store.put(event, artifacts={"stdout": b"different"})
        assert store.read_artifact(event.event_id, "stdout") == content
    with Store(tmp_path, event.project_id) as store:
        assert store.read_artifact(event.event_id, "stdout") == content


def test_artifact_write_failure_does_not_commit_event(tmp_path, event, monkeypatch):
    with Store(tmp_path, event.project_id) as store:

        def fail_replace(*args):
            raise OSError("Simulated disk failure")

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError):
            store.put(event, artifacts={"stdout": b"output"})
        assert store.events() == []
        assert list((tmp_path / "artifacts").glob("*.tmp")) == []


def test_symlink_input_is_removed_without_reading_target(tmp_path, event):
    inbox = Inbox(tmp_path / "project")
    inbox.publish(event)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"private": "data"}))
    alias = inbox.directory / "alias.json"
    try:
        alias.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")
    with Store(tmp_path / "project", event.project_id) as store:
        result = inbox.drain(store)
        assert result.discarded == 1
        assert result.inserted == 1
    assert outside.exists()
    assert not alias.is_symlink()


def test_drain_requires_an_open_writer_even_for_invalid_input(tmp_path, event):
    inbox = Inbox(tmp_path)
    incoming = inbox.publish(event)
    incoming.write_bytes(b"invalid")
    with pytest.raises(StorageError):
        inbox.drain(Store(tmp_path, event.project_id))
    assert incoming.exists()
    assert not (tmp_path / "quarantine").exists()


def test_artifact_reference_failure_rolls_back_event_and_reports_unknown_artifact(tmp_path, event):
    with Store(tmp_path, event.project_id) as store:
        store.connection.execute(
            "CREATE TRIGGER fail_reference BEFORE INSERT ON artifacts "
            "BEGIN SELECT RAISE(ABORT, 'Simulated reference failure'); END"
        )
        with pytest.raises(sqlite3.DatabaseError):
            store.put(event, artifacts={"output": b"content"})
        assert store.events() == []
        with pytest.raises(StorageError):
            store.read_artifact(event.event_id, "output")
        store.connection.execute("DROP TRIGGER fail_reference")
        assert store.put(event, artifacts={"output": b"content"})
        assert store.read_artifact(event.event_id, "output") == b"content"


def test_read_only_connection_can_read_while_writer_is_open(tmp_path, event):
    with Store(tmp_path, event.project_id) as store:
        store.put(event)
        reader = sqlite3.connect((tmp_path / "events.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("DELETE FROM events")
        finally:
            reader.close()


def test_corrupt_artifact_is_detected_and_artifact_limit_is_enforced(tmp_path, event):
    with Store(tmp_path, event.project_id, artifact_bytes=4) as store:
        with pytest.raises(StorageError):
            store.put(event, artifacts={"output": b"too large"})
        assert store.events() == []
        store.put(event, artifacts={"output": b"safe"})
        next((tmp_path / "artifacts").glob("*.bin")).write_bytes(b"bad!")
        with pytest.raises(StorageError):
            store.read_artifact(event.event_id, "output")


def test_interrupted_initial_migration_rolls_back_and_can_restart(tmp_path, event):
    script = Path(__file__).parent / "storage_crash_probe.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(event.project_id), "migration"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 91, result.stderr
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()
    with Store(tmp_path, event.project_id) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert store.put(event)


@pytest.mark.parametrize("version", [1, 2])
def test_v1_and_v2_databases_migrate_transcript_reader_state(tmp_path, event, version):
    with Store(tmp_path, event.project_id) as store:
        store.connection.execute("DROP TABLE transcript_sources")
        store.connection.execute("DROP TABLE diff_snapshots")
        if version == 1:
            store.connection.execute("DROP TABLE receipts")
            store.connection.execute("DROP TABLE pins")
        store.connection.execute(f"PRAGMA user_version={version}")
    with Store(tmp_path, event.project_id) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert store.transcript_sources() == []


@pytest.mark.parametrize("missing", ["event_id", "received_at", "schema_version"])
def test_persisted_envelope_cannot_generate_new_identity_on_replay(tmp_path, event, missing):
    inbox = Inbox(tmp_path)
    path = inbox.publish(event)
    raw = json.loads(path.read_bytes())
    del raw[missing]
    path.write_text(json.dumps(raw))
    with Store(tmp_path, event.project_id) as store:
        assert inbox.drain(store).quarantined == 1
        assert store.events() == []
