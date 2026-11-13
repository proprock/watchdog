"""Offline behavioral tests for hook event imports."""

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from agent_watchdog import capture_store, config, readapi
from wdi.db.connection import connect
from wdi.db.repository import Repository


def _store(machine="machine-a", project="project-a"):
    return SimpleNamespace(
        machine_id=machine, project_id=project, hostname="host", os="test", supported=True
    )


def _event(rowid, event_id):
    return {
        "event_rowid": rowid,
        "event_id": event_id,
        "received_at": "2026-01-01T00:00:00Z",
        "session_id": "session",
        "kind": "tool",
        "envelope": '{"synthetic":true}',
    }


@pytest.fixture
def repository(tmp_path: Path):
    conn = connect(tmp_path / "insights.sqlite")
    yield Repository(conn)
    conn.close()


def _readers(monkeypatch, records, *, fail_facts=False):
    def events(info, after_rowid=0):
        return iter(
            [
                row
                for row in records[(info.machine_id, info.project_id)]
                if row["event_rowid"] > after_rowid
            ]
        )

    def facts(info, after_rowid=0):
        for row in events(info, after_rowid):
            if fail_facts:
                raise ValueError("synthetic read failure")
            fact = dict.fromkeys(readapi.FACT_COLUMNS)
            fact.update(event_rowid=row["event_rowid"], event_id=row["event_id"], tool_name="Bash")
            yield fact

    monkeypatch.setattr(readapi, "events", events, raising=False)
    monkeypatch.setattr(readapi, "event_facts", facts, raising=False)


def test_incremental_import_is_idempotent(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    records = {("machine-a", "project-a"): [_event(1, "one")]}
    _readers(monkeypatch, records)
    assert import_hook_facts(repository, [store]) == 1
    assert import_hook_facts(repository, [store]) == 0
    records[("machine-a", "project-a")].append(_event(3, "two"))
    assert import_hook_facts(repository, [store]) == 1
    assert [
        tuple(row)
        for row in repository.conn.execute(
            "SELECT event_rowid, event_id FROM hook_events ORDER BY event_rowid"
        )
    ] == [(1, "one"), (3, "two")]
    assert [
        tuple(row)
        for row in repository.conn.execute(
            "SELECT event_id, tool_name FROM hook_facts ORDER BY event_rowid"
        )
    ] == [("one", "Bash"), ("two", "Bash")]
    assert (
        repository.get_import_state("machine-a", "wd-facts://machine-a/project-a").byte_offset == 3
    )


def test_full_refresh_replaces_stale_projection(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    records = {("machine-a", "project-a"): [_event(1, "old")]}
    _readers(monkeypatch, records)
    import_hook_facts(repository, [store])
    records[("machine-a", "project-a")] = [_event(1, "new")]
    assert import_hook_facts(repository, [store], full=True) == 1
    assert [row[0] for row in repository.conn.execute("SELECT event_id FROM hook_events")] == [
        "new"
    ]
    assert [row[0] for row in repository.conn.execute("SELECT event_id FROM hook_facts")] == ["new"]


def test_equal_event_ids_are_scoped_by_machine_and_project(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    stores = [
        _store("machine-a", "project-a"),
        _store("machine-a", "project-b"),
        _store("machine-b", "project-a"),
    ]
    records = {(s.machine_id, s.project_id): [_event(1, "same")] for s in stores}
    _readers(monkeypatch, records)
    assert import_hook_facts(repository, stores) == 3
    assert {
        tuple(row)
        for row in repository.conn.execute(
            "SELECT machine_id, project_id, event_id FROM hook_events"
        )
    } == {(s.machine_id, s.project_id, "same") for s in stores}
    assert repository.conn.execute("SELECT count(*) FROM hook_facts").fetchone()[0] == 3


def test_read_failure_rolls_back_projection_and_cursor(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    records = {("machine-a", "project-a"): [_event(1, "one")]}
    _readers(monkeypatch, records)
    import_hook_facts(repository, [store])
    records[("machine-a", "project-a")].append(_event(2, "two"))
    _readers(monkeypatch, records, fail_facts=True)
    with pytest.raises(ValueError, match="synthetic read failure"):
        import_hook_facts(repository, [store])
    assert [row[0] for row in repository.conn.execute("SELECT event_id FROM hook_events")] == [
        "one"
    ]
    assert (
        repository.get_import_state("machine-a", "wd-facts://machine-a/project-a").byte_offset == 1
    )


def test_absent_events_database_is_empty(repository, tmp_path):
    from wdi.ingest.hook_facts import import_hook_facts

    project_id = UUID("22222222-2222-4222-8222-222222222222")
    project_dir = tmp_path / "data" / "projects" / str(project_id)
    project_dir.mkdir(parents=True)
    capture_store.write_manifest(
        project_dir,
        machine_id=UUID("11111111-1111-4111-8111-111111111111"),
        project=config.Project(id=project_id, root=tmp_path),
    )
    stores = readapi.stores(tmp_path / "data")
    assert import_hook_facts(repository, stores) == 0
    assert repository.conn.execute("SELECT count(*) FROM hook_events").fetchone()[0] == 0
