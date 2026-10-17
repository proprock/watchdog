"""Retention work selects in SQL and yields without losing its finite scan."""

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_watchdog import storage
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.storage import MaintenanceBudget, MaintenancePending, MaintenanceState, Store


def event(project, days=0, session="session"):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        kind="tool.finish",
        source="hook",
        received_at=datetime.now(UTC) - timedelta(days=days),
        payload={"codex": {"content": {"prompt": "expired"}}},
    )


def test_only_unstripped_cutoff_content_is_parsed(tmp_path, monkeypatch):
    project = uuid4()
    recent, content, metrics, pinned = (
        event(project),
        event(project, 40),
        event(project, 200),
        event(project, 200, "pinned"),
    )
    with Store(tmp_path, project) as store:
        for item in (recent, content, metrics, pinned):
            store.put(item)
        store.pin("pinned")
        parsed = []
        original = storage.persisted_envelope

        def parse(document):
            result = original(document)
            parsed.append(result.event_id)
            return result

        monkeypatch.setattr(storage, "persisted_envelope", parse)
        result = store.maintain()
        assert not result.pending and result.space_available
        assert parsed == [content.event_id]
        store.maintain()
        assert parsed == [content.event_id]
        ids = {row[0] for row in store.connection.execute("SELECT event_id FROM events")}
        assert ids == {str(item.event_id) for item in (recent, content, pinned)}


def test_sql_cutoff_compares_instants_across_timezone_offsets(tmp_path):
    project = uuid4()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    expired = event(project).model_copy(
        update={"received_at": datetime.fromisoformat("2026-09-17T01:00:00+02:00")}
    )
    recent = event(project).model_copy(
        update={"received_at": datetime.fromisoformat("2026-09-16T23:00:00-02:00")}
    )
    with Store(tmp_path, project) as store:
        store.put(expired)
        store.put(recent)
        store.maintain(now=now)
        retained = {item.event_id: item for item in store.events()}
    assert retained[expired.event_id].payload == {}
    assert retained[recent.event_id].payload == recent.payload


def test_expired_content_is_committed_in_bounded_groups(tmp_path):
    project = uuid4()
    with Store(tmp_path, project) as store:
        expired = [event(project, 40) for _ in range(70)]
        with store.batch():
            for item in expired:
                store.put(item)
        commits = []
        store.connection.set_trace_callback(
            lambda statement: commits.append(statement) if statement == "COMMIT" else None
        )
        assert not store.maintain().pending
        assert len(commits) < len(expired)
        assert all(not item.payload for item in store.events())


def test_expired_deadline_does_no_work_and_resumes_across_store_opens(tmp_path, monkeypatch):
    project = uuid4()
    progress = MaintenanceState()
    with Store(tmp_path, project, maintenance_state=progress) as store:
        store.put(event(project, 40))
        parsed = []
        original = storage.persisted_envelope

        def parse(document):
            parsed.append(document)
            return original(document)

        monkeypatch.setattr(storage, "persisted_envelope", parse)
        result = store.maintain(deadline=time.monotonic() - 1)
        assert result.pending and not result.exhausted
        assert parsed == []
    with Store(tmp_path, project, maintenance_state=progress) as store:
        assert not store.maintain().pending
        assert len(parsed) == 1


def test_interrupted_content_scan_finishes_without_parsing_twice(tmp_path, monkeypatch):
    project = uuid4()
    progress = MaintenanceState()
    with Store(tmp_path, project, maintenance_state=progress) as store:
        for _ in range(70):
            store.put(event(project, 40))
        clock = [0.0]
        parsed = []
        original = storage.persisted_envelope

        def parse(document):
            result = original(document)
            parsed.append(result.event_id)
            clock[0] += 1.0
            return result

        monkeypatch.setattr(storage, "persisted_envelope", parse)
        monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])
        assert store.maintain(deadline=1.0).pending
        assert 0 < len(parsed) < 70
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE "
            "json_extract(envelope, '$.availability.content')='unavailable'"
        ).fetchone()[0] == len(parsed)
    with Store(tmp_path, project, maintenance_state=progress) as store:
        for _ in range(100):
            result = store.maintain(deadline=clock[0] + 1.0)
            if not result.pending:
                break
        else:
            raise AssertionError("Maintenance never completed its finite scan")
    assert len(parsed) == len(set(parsed)) == 70


def test_quota_cleanup_deadline_is_transient(tmp_path, monkeypatch):
    project = uuid4()
    with Store(tmp_path, project) as store:
        store.put(event(project))
        monkeypatch.setattr(storage.resources, "available", lambda *_: 0)
        store.maintenance_deadline = time.monotonic() - 1
        try:
            store.put(event(project))
        except MaintenancePending:
            pass
        else:
            raise AssertionError("Quota cleanup must defer when its tick has expired")


def test_pinned_quota_exhaustion_is_final(tmp_path):
    project = uuid4()
    with Store(tmp_path, project) as store:
        store.put(event(project))
        store.pin("session")
    limits = Limits(project_bytes=100_000, reserve_bytes=10_000, payload_bytes=6000)
    with Store(tmp_path, project, limits=limits) as store:
        result = store.maintain()
        assert not result.pending and result.exhausted and not result.space_available


def test_shared_budget_charges_cleanup_time_only_and_never_forces_a_chunk(tmp_path, monkeypatch):
    project = uuid4()
    with Store(tmp_path, project) as store:
        for _ in range(3):
            store.put(event(project, 40))
        clock, parsed = [0.0], []
        original = storage.persisted_envelope

        def parse(document):
            parsed.append(document)
            clock[0] += 0.15
            return original(document)

        monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(storage, "persisted_envelope", parse)
        budget = MaintenanceBudget()
        store.maintenance_budget = budget
        assert store.maintain().pending
        completed = len(parsed)
        assert completed > 0 and budget.remaining == 0
        clock[0] += 20  # Work elsewhere in this tick cannot refill the allowance.
        assert store.maintain().pending
        assert len(parsed) == completed


def test_files_and_accounting_resume_without_unbounded_available_scan(tmp_path, monkeypatch):
    project = uuid4()
    progress = MaintenanceState()
    with Store(tmp_path, project, maintenance_state=progress) as store:
        directory = tmp_path / "artifacts"
        directory.mkdir(exist_ok=True)
        for index in range(80):
            (directory / f"{index:064x}.bin").write_bytes(b"orphan")
        monkeypatch.setattr(
            storage.resources,
            "available",
            lambda *_: (_ for _ in ()).throw(AssertionError("Unbounded accounting")),
        )
        clock = [0.0]

        def now():
            clock[0] += 0.001
            return clock[0]

        monkeypatch.setattr(storage.time, "monotonic", now)
        calls = 0
        while True:
            calls += 1
            result = store.maintain(deadline=clock[0] + 0.01)
            if not result.pending:
                break
            assert calls < 100
        assert calls > 1 and result.space_available
        assert not list(directory.glob("*.bin"))


def test_reader_blocked_checkpoint_remains_pending_until_reader_releases(tmp_path):
    project = uuid4()
    with Store(tmp_path, project) as store:
        store.put(event(project, 200))
        with sqlite3.connect(tmp_path / "events.sqlite3") as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT event_id FROM events").fetchall()
            result = store.maintain()
            assert result.pending and not result.exhausted
            reader.rollback()
        result = store.maintain()
        assert not result.pending and result.space_available


def test_pressure_strips_oldest_unpinned_content_and_stops_when_space_returns(
    tmp_path, monkeypatch
):
    project = uuid4()
    recent, old, pinned = event(project), event(project, 100), event(project, 200, "pinned")
    with Store(tmp_path, project, limits=Limits(content_days=0, metrics_days=0)) as store:
        for item in (recent, old, pinned):
            store.put(item)
        store.pin("pinned")
        disk = storage.shutil.disk_usage(tmp_path)

        def available(_):
            stripped = (
                store.connection.execute(
                    "SELECT json_extract(envelope, '$.availability.content') FROM events "
                    "WHERE event_id=?",
                    (str(old.event_id),),
                ).fetchone()[0]
                == "unavailable"
            )
            return disk._replace(free=10**12 if stripped else 0)

        monkeypatch.setattr(storage.shutil, "disk_usage", available)
        result = store.maintain()
        assert result.space_available and not result.pending
        remaining = {item.event_id: item for item in store.events()}
        assert remaining[old.event_id].payload == {}
        assert remaining[recent.event_id] == recent
        assert remaining[pinned.event_id] == pinned
