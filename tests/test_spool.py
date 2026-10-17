"""One durable queue for provider observations and internal envelopes."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from helpers.spool import PreparedQueue as Spool

from agent_watchdog.events import Envelope
from agent_watchdog.storage import DrainResult, MaintenanceBudget, Store, WriterBusy


def test_commit_before_unlink_and_replay_is_one_event(tmp_path, monkeypatch):
    event = Envelope(provider="codex", project_id=uuid4(), kind="control", source="daemon")
    queue = Spool(tmp_path)
    path = queue.publish(event)
    assert json.loads(path.read_bytes())["record_type"] == "envelope"
    original = Path.unlink
    with Store(tmp_path / "project", event.project_id) as store:

        def crash(path, *args, **kwargs):
            if path.parent == queue.directory:
                assert not store.connection.in_transaction
                raise OSError("crash after commit")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", crash)
            with pytest.raises(OSError, match="after commit"):
                queue.drain(store)
        assert path.exists() and len(store.events()) == 1
        result = queue.drain(store)
        assert (result.inserted, result.duplicates) == (0, 1)
        assert len(store.events()) == 1 and not path.exists()


def test_legacy_record_is_rejected_without_permanent_reader(tmp_path):
    queue = Spool(tmp_path)
    queue.directory.mkdir(parents=True)
    (queue.directory / "old.json").write_text('{"schema_version":1}')
    with Store(tmp_path / "project", uuid4()) as store:
        result = queue.drain(store)
    assert result.quarantined == 1
    assert not list(queue.directory.glob("*.json"))


def test_footprint_tolerates_commit_unlink_and_counts_crash_temporaries(tmp_path, monkeypatch):
    queue = Spool(tmp_path)
    queue.directory.mkdir()
    gone = queue.directory / "gone.json"
    gone.write_bytes(b"gone")
    (queue.directory / "crash.tmp").write_bytes(b"partial")
    (queue.directory / "limits.json").write_bytes(b"not a pending record")
    original = Path.lstat

    def raced(path, *args, **kwargs):
        if path == gone:
            gone.unlink(missing_ok=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", raced)
    assert queue.footprint() == (1, 7)


def test_temporary_cleanup_uses_shared_budget_and_preserves_recent_writes(tmp_path):
    queue = Spool(tmp_path)
    queue.directory.mkdir()
    old = queue.directory / "old.tmp"
    recent = queue.directory / "recent.tmp"
    record = queue.directory / "event.json"
    for path in (old, recent, record):
        path.write_bytes(b"pending")
    timestamp = time.time() - 7200
    os.utime(old, (timestamp, timestamp))
    assert queue.maintain(MaintenanceBudget(0))
    assert old.exists()
    assert not queue.maintain(MaintenanceBudget(1))
    assert not old.exists() and recent.exists() and record.exists()


def test_retained_window_does_not_starve_later_queue_records(tmp_path):
    queue = Spool(tmp_path)
    for _ in range(5):
        queue.publish(
            Envelope(provider="codex", project_id=uuid4(), kind="control", source="daemon")
        )
    first = {path for path, _ in queue.records(DrainResult(), limit=2)}
    second = {path for path, _ in queue.records(DrainResult(), limit=2)}
    third = {path for path, _ in queue.records(DrainResult(), limit=2)}
    assert len(first | second | third) == 5
    assert first.isdisjoint(second) and len(third) == 1


def test_busy_quarantine_preserves_invalid_record_and_reads_later_input(tmp_path, monkeypatch):
    queue = Spool(tmp_path)
    queue.directory.mkdir()
    bad = queue.directory / "bad.json"
    bad.write_bytes(b"malformed")
    good = queue.publish(
        Envelope(provider="codex", project_id=uuid4(), kind="control", source="daemon")
    )

    def busy(*args):
        raise WriterBusy("publisher active")

    monkeypatch.setattr(queue, "quarantine", busy)
    assert [path for path, _ in queue.records(DrainResult())] == [good]
    assert bad.exists()
