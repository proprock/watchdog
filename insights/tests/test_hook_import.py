"""Offline behavioral tests for hook event imports."""

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository

from agent_watchdog import capture_store, config, readapi


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


def test_incremental_import_refreshes_late_codex_attribution(tmp_path):
    from datetime import UTC, datetime, timedelta

    from wdi.ingest.hook_facts import import_hook_facts

    from agent_watchdog.events import Envelope
    from agent_watchdog.storage import Store

    project_id = UUID("22222222-2222-4222-8222-222222222222")
    machine_id = UUID("11111111-1111-4111-8111-111111111111")
    project = config.Project(id=project_id, root=tmp_path)
    project_dir = tmp_path / "watchdog" / "projects" / str(project_id)
    base = datetime(2026, 10, 10, tzinfo=UTC)

    def event(kind, seconds, *, record_type=None, **metadata):
        payload = {"metadata": metadata}
        if kind == "usage":
            payload["usage"] = {"delta": {"input_tokens": 123, "output_tokens": 45}}
        if record_type:
            payload["metadata"]["record_type"] = record_type
        return Envelope(
            provider="codex",
            project_id=project_id,
            session_id="session",
            turn_id="turn",
            kind=kind,
            source="transcript",
            occurred_at=base + timedelta(seconds=seconds),
            received_at=base + timedelta(days=1, seconds=seconds),
            payload={"codex": payload},
        )

    usage = event("usage", 5)
    with Store(project_dir, project_id) as source:
        source.put(usage)
    capture_store.write_manifest(project_dir, machine_id=machine_id, project=project)
    stores = readapi.stores(tmp_path / "watchdog")
    conn = connect(tmp_path / "insights.sqlite")
    repo = Repository(conn)
    try:
        assert import_hook_facts(repo, stores) == 1
        raw_before = conn.execute(
            "SELECT e.envelope, f.input_tokens, f.output_tokens FROM hook_events e "
            "JOIN hook_facts f USING (machine_id, project_id, event_rowid) "
            "WHERE e.event_id = ?",
            (str(usage.event_id),),
        ).fetchone()
        assert raw_before[1:] == (123, 45)

        with Store(project_dir, project_id) as source:
            source.put(
                event(
                    "unknown",
                    1,
                    record_type="turn_context",
                    attribution_projection="wd-165.v1",
                    model="gpt-test",
                    reasoning_effort="high",
                )
            )
        assert import_hook_facts(repo, stores) == 1

        fact = conn.execute(
            "SELECT model, model_attribution, reasoning_effort, "
            "reasoning_effort_attribution, input_tokens, output_tokens "
            "FROM hook_facts WHERE event_id = ?",
            (str(usage.event_id),),
        ).fetchone()
        assert tuple(fact) == ("gpt-test", "inherited", "high", "inherited", 123, 45)
        raw_after = conn.execute(
            "SELECT envelope FROM hook_events WHERE event_id = ?", (str(usage.event_id),)
        ).fetchone()[0]
        assert raw_after == raw_before[0]
    finally:
        conn.close()


def test_incremental_refresh_is_bounded_and_wraps_frozen_cycles(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    key = ("machine-a", "project-a")
    records = {key: [_event(rowid, f"event-{rowid}") for rowid in range(1, 1201)]}
    source_facts = {
        row["event_rowid"]: {
            **dict.fromkeys(readapi.FACT_COLUMNS),
            "event_rowid": row["event_rowid"],
            "event_id": row["event_id"],
            "model": "old-model",
            "model_attribution": "observed",
        }
        for row in records[key]
    }
    call_id = [0]
    refreshed_per_call = {}

    def events(info, after_rowid=0):
        return iter(row for row in records[key] if row["event_rowid"] > after_rowid)

    def facts(info, after_rowid=0):
        for rowid, row in sorted(source_facts.items()):
            if rowid > after_rowid:
                if rowid <= 1200:
                    refreshed_per_call[call_id[0]] = refreshed_per_call.get(call_id[0], 0) + 1
                yield row

    monkeypatch.setattr(readapi, "events", events, raising=False)
    monkeypatch.setattr(readapi, "event_facts", facts, raising=False)
    assert import_hook_facts(repository, [store], full=True) == 1200
    refresh_key = "wd-facts-refresh://machine-a/project-a"
    assert repository.get_import_state("machine-a", refresh_key).byte_offset == 0
    assert repository.get_import_state("machine-a", refresh_key).file_size == 1200

    for row in source_facts.values():
        row["model"] = "late-model"
        row["model_attribution"] = "inherited"

    for added in range(1201, 1204):
        records[key].append(_event(added, f"event-{added}"))
        source_facts[added] = {
            **dict.fromkeys(readapi.FACT_COLUMNS),
            "event_rowid": added,
            "event_id": f"event-{added}",
            "model": "new-model",
            "model_attribution": "observed",
        }
        before = repository.conn.execute(
            "SELECT count(*) FROM hook_facts WHERE event_rowid <= 1200 AND model = 'late-model'"
        ).fetchone()[0]
        call_id[0] += 1
        assert import_hook_facts(repository, [store]) == 1
        assert refreshed_per_call[call_id[0]] <= 500
        after = repository.conn.execute(
            "SELECT count(*) FROM hook_facts WHERE event_rowid <= 1200 AND model = 'late-model'"
        ).fetchone()[0]
        assert after - before <= 500
        if added == 1201:
            source_facts[1]["model"] = "visited-again"

    state = repository.get_import_state("machine-a", refresh_key)
    assert state.byte_offset == 0
    assert state.file_size == 0

    assert (
        repository.conn.execute("SELECT model FROM hook_facts WHERE event_rowid = 1200").fetchone()[
            0
        ]
        == "late-model"
    )
    assert (
        repository.conn.execute("SELECT model FROM hook_facts WHERE event_rowid = 1").fetchone()[0]
        == "late-model"
    )
    assert (
        repository.conn.execute("SELECT model FROM hook_facts WHERE event_rowid = 1201").fetchone()[
            0
        ]
        == "new-model"
    )
    records[key].append(_event(1204, "event-1204"))
    source_facts[1204] = {
        **dict.fromkeys(readapi.FACT_COLUMNS),
        "event_rowid": 1204,
        "event_id": "event-1204",
        "model": "new-model",
        "model_attribution": "observed",
    }
    call_id[0] += 1
    assert import_hook_facts(repository, [store]) == 1
    assert refreshed_per_call[call_id[0]] <= 500
    assert (
        repository.conn.execute("SELECT model FROM hook_facts WHERE event_rowid = 1").fetchone()[0]
        == "visited-again"
    )


def test_refresh_failure_rolls_back_attribution_cursors_and_new_events(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    key = ("machine-a", "project-a")
    records = {key: [_event(1, "one")]}
    _readers(monkeypatch, records)
    assert import_hook_facts(repository, [store]) == 1
    refresh_key = "wd-facts-refresh://machine-a/project-a"
    repository.set_import_state("machine-a", refresh_key, file_size=1, file_mtime=0, byte_offset=0)
    before = repository.get_import_state("machine-a", refresh_key)
    records[key].append(_event(2, "two"))

    def facts(info, after_rowid=0):
        for rowid, event_id in ((1, "one"), (2, "two")):
            if rowid > after_rowid:
                yield {
                    **dict.fromkeys(readapi.FACT_COLUMNS),
                    "event_rowid": rowid,
                    "event_id": event_id,
                    "model": "late" if rowid == 1 else None,
                    "model_attribution": "inherited" if rowid == 1 else "unavailable",
                }

    monkeypatch.setattr(readapi, "event_facts", facts, raising=False)
    original_set_state = repository.set_import_state

    def fail_refresh_cursor(machine_id, source_path, **kwargs):
        original_set_state(machine_id, source_path, **kwargs)
        if source_path == refresh_key:
            raise ValueError("synthetic refresh failure")

    monkeypatch.setattr(repository, "set_import_state", fail_refresh_cursor)
    with pytest.raises(ValueError, match="synthetic refresh failure"):
        import_hook_facts(repository, [store])
    assert [
        row[0]
        for row in repository.conn.execute("SELECT event_id FROM hook_events ORDER BY event_rowid")
    ] == ["one"]
    assert (
        repository.conn.execute("SELECT model FROM hook_facts WHERE event_rowid = 1").fetchone()[0]
        is None
    )
    assert (
        repository.get_import_state("machine-a", "wd-facts://machine-a/project-a").byte_offset == 1
    )
    assert repository.get_import_state("machine-a", refresh_key) == before


def test_full_import_resets_refresh_progress_and_updates_all_facts(repository, monkeypatch):
    from wdi.ingest.hook_facts import import_hook_facts

    store = _store()
    key = ("machine-a", "project-a")
    records = {key: [_event(rowid, f"event-{rowid}") for rowid in range(1, 4)]}
    source_facts = {
        row["event_rowid"]: {
            **dict.fromkeys(readapi.FACT_COLUMNS),
            "event_rowid": row["event_rowid"],
            "event_id": row["event_id"],
            "model": "refreshed",
            "model_attribution": "inherited",
        }
        for row in records[key]
    }
    _readers(monkeypatch, records)
    assert import_hook_facts(repository, [store]) == 3
    refresh_key = "wd-facts-refresh://machine-a/project-a"
    repository.set_import_state(
        "machine-a", refresh_key, file_size=50, file_mtime=0, byte_offset=25
    )

    def facts(info, after_rowid=0):
        yield from (row for rowid, row in sorted(source_facts.items()) if rowid > after_rowid)

    monkeypatch.setattr(readapi, "event_facts", facts, raising=False)
    assert import_hook_facts(repository, [store], full=True) == 3
    state = repository.get_import_state("machine-a", refresh_key)
    assert state.byte_offset == 0
    assert state.file_size == 3
    assert [
        tuple(row)
        for row in repository.conn.execute(
            "SELECT model FROM hook_facts ORDER BY event_rowid"
        ).fetchall()
    ] == [("refreshed",), ("refreshed",), ("refreshed",)]


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


def test_normal_import_includes_event_only_store(tmp_path):
    from wdi.config import Settings
    from wdi.ingest.service import run_import

    from agent_watchdog.events import Envelope
    from agent_watchdog.storage import Store

    machine = UUID("11111111-1111-4111-8111-111111111111")
    project = config.Project(id=UUID("22222222-2222-4222-8222-222222222222"), root=tmp_path)
    data = tmp_path / "watchdog"
    project_dir = data / "projects" / str(project.id)
    with Store(project_dir, project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id="hook-only",
                kind="tool.finish",
                source="hook",
                payload={"claude": {"metadata": {"tool_name": "Read"}}},
            )
        )
    capture_store.write_manifest(project_dir, machine_id=machine, project=project)
    settings = Settings(
        machine_id=str(machine),
        db_path=tmp_path / "derived.sqlite",
        sources={
            "claude_code": {"enabled": False, "root": tmp_path / "missing-claude"},
            "codex": {"enabled": False, "root": tmp_path / "missing-codex"},
        },
        watchdog={"data_dirs": [data]},
    )

    results = run_import(settings)
    assert results["watchdog"].warnings == 0
    with connect(settings.db_path) as db:
        assert db.execute("SELECT session_id, tool_name FROM hook_facts").fetchone()[:] == (
            "hook-only",
            "Read",
        )
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_conversion_preserves_primary_data_and_revision_two_backup(tmp_path, monkeypatch):
    import runpy
    import sqlite3
    import sys
    from contextlib import closing

    database = tmp_path / "insights.sqlite"
    backup = tmp_path / "backup.sqlite"
    conn = connect(database)
    Repository(conn).ensure_machine("machine")
    conn.execute(
        "INSERT INTO sessions (session_id, machine_id, agent) VALUES ('saved', 'machine', 'claude')"
    )
    conn.execute(
        "INSERT INTO llm_analyses (session_id, machine_id, analysis_type, prompt_version, "
        "model, created_at, result) VALUES "
        "('saved', 'machine', 'summary', 'v1', 'haiku', '2026-01-01', '{}')"
    )
    conn.execute("DROP TABLE hook_facts")
    conn.execute("DROP TABLE hook_events")
    conn.execute("DROP TABLE session_env")
    conn.execute("DROP TABLE checkout_states")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    script = Path(__file__).parents[2] / "scripts/wd195_convert_insights.py"
    monkeypatch.setattr(sys, "argv", [str(script), str(database), str(backup)])

    runpy.run_path(str(script), run_name="__main__")

    with (
        closing(sqlite3.connect(database)) as converted,
        closing(sqlite3.connect(backup)) as previous,
    ):
        assert converted.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            converted.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE name IN ('session_env', 'checkout_states')"
            ).fetchone()[0]
            == 0
        )
        assert converted.execute("SELECT session_id FROM llm_analyses").fetchone()[0] == "saved"
        assert converted.execute("SELECT count(*) FROM hook_facts").fetchone()[0] == 0
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 2
        assert previous.execute("SELECT session_id FROM llm_analyses").fetchone()[0] == "saved"
