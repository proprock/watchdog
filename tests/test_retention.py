from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog import resources
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Inbox, StorageError, Store


def event(project, days=0, session="session"):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        kind="tool.finish",
        source="hook",
        received_at=datetime.now(UTC) - timedelta(days=days),
        payload={"codex": {"content": {"prompt": "x" * 4096}}},
    )


def test_expiry_preserves_pins_and_replay_identity(tmp_path):
    project = uuid4()
    recent, old, pinned = (
        event(project, 40),
        event(project, 200, "old"),
        event(project, 200, "pinned"),
    )
    with Store(tmp_path, project) as store:
        for item in (recent, old, pinned):
            store.put(item, artifacts={"stdout": b"text"})
        store.pin("pinned")
        store.maintain()
        remaining = {item.event_id: item for item in store.events()}
        assert old.event_id not in remaining
        assert remaining[pinned.event_id] == pinned
        assert "xxxx" not in remaining[recent.event_id].model_dump_json()
        assert not store.put(recent, artifacts={"stdout": b"text"})
        store.pin("pinned", pinned=False)
        store.maintain()
        assert len(store.events()) == 1


def test_concurrent_publish_obeys_inbox_quota(tmp_path):
    limits = Limits(payload_bytes=6000, inbox_bytes=12000)
    inbox = Inbox(tmp_path, limits=limits)
    item = event(uuid4())

    def publish(_):
        try:
            inbox.publish(item)
            return True
        except StorageError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(24)))
    assert 0 < sum(results) < 24
    assert sum(p.stat().st_size for p in inbox.directory.iterdir()) <= limits.inbox_bytes


def test_cleanup_reclaims_database_and_only_owned_files(tmp_path):
    project = uuid4()
    with Store(tmp_path, project) as store:
        for _ in range(20):
            store.put(event(project, 200))
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = (tmp_path / "events.sqlite3").stat().st_size
        orphan = tmp_path / "artifacts" / ("a" * 64 + ".bin")
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"orphan")
        vendor = tmp_path / "vendor-transcript.jsonl"
        vendor.write_text("preserve")
        store.maintain()
        assert not orphan.exists()
        assert vendor.read_text() == "preserve"
        assert (tmp_path / "events.sqlite3").stat().st_size < before


def test_pinned_saturation_refuses_new_data(tmp_path):
    project = uuid4()
    with Store(tmp_path, project) as store:
        item = event(project)
        store.put(item)
        store.pin("session")
    limits = Limits(
        project_bytes=100_000, reserve_bytes=10_000, inbox_bytes=10_000, payload_bytes=6000
    )
    with Store(tmp_path, project, limits=limits) as store:
        with pytest.raises(StorageError):
            store.put(event(project))
        assert store.events() == [item]


def test_disk_full_keeps_queue_empty_and_loss_survives_reopen(tmp_path, monkeypatch):
    resources.count_loss(tmp_path, "io", 0)

    def full(*args):
        raise OSError(28, "Simulated disk full")

    monkeypatch.setattr("agent_watchdog.storage.atomic_write", full)
    with pytest.raises(OSError):
        Inbox(tmp_path).publish(event(uuid4()))
    assert not list((tmp_path / "inbox").glob("*.json"))
    assert resources.losses(tmp_path)["io"] == 1


def test_quota_losses_are_exact_under_concurrent_publish(tmp_path):
    limits = Limits(payload_bytes=6000, inbox_bytes=6000)
    inbox = Inbox(tmp_path, limits=limits)
    inbox.publish(event(uuid4()))

    def rejected(_):
        with pytest.raises(StorageError):
            inbox.publish(event(uuid4()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(rejected, range(20)))
    assert sum(resources.losses(tmp_path).values()) == 20


def test_stale_temporary_cleanup_preserves_recent_files(tmp_path):
    import os

    stale, recent = tmp_path / "stale.tmp", tmp_path / "recent.tmp"
    stale.write_bytes(b"unfinished")
    recent.write_bytes(b"current")
    os.utime(stale, (1, 1))
    with Store(tmp_path, uuid4()) as store:
        store.maintain()
    assert not stale.exists() and recent.exists()


def test_v1_upgrade_preserves_replay_and_enables_reclamation(tmp_path):
    import sqlite3

    from agent_watchdog.storage import canonical

    project = uuid4()
    item = event(project)
    db = sqlite3.connect(tmp_path / "events.sqlite3")
    try:
        db.executescript("""
            CREATE TABLE metadata(project_id TEXT NOT NULL);
            CREATE TABLE events(event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                                session_id TEXT, kind TEXT NOT NULL, envelope TEXT NOT NULL);
            CREATE TABLE artifacts(event_id TEXT NOT NULL REFERENCES events(event_id),
                                   name TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
                                   PRIMARY KEY(event_id, name));
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO metadata VALUES (?)", (str(project),))
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (
                str(item.event_id),
                item.received_at.isoformat(),
                item.session_id,
                item.kind,
                canonical(item),
            ),
        )
        db.commit()
    finally:
        db.close()
    with Store(tmp_path, project) as store:
        assert store.connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert not store.put(item)
        assert store.events() == [item]


def test_quota_accounts_for_wal_and_auxiliary_files(tmp_path):
    project = uuid4()
    limits = Limits(
        project_bytes=700_000, reserve_bytes=100_000, inbox_bytes=100_000, payload_bytes=10_000
    )
    with Store(tmp_path, project, limits=limits) as store:
        store.put(event(project))
        store.pin("session")
        auxiliary = tmp_path / "diagnostic.log"
        auxiliary.write_bytes(b"x" * 200_000)
        for _ in range(100):
            try:
                store.put(event(project))
            except StorageError:
                break
        else:
            pytest.fail("Pinned project never reached quota")
        assert resources.usage(tmp_path) <= limits.project_bytes
        assert auxiliary.exists()
