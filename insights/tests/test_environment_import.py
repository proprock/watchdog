"""Offline checks for importing captured environment snapshots."""

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from wdi.config import Settings
from wdi.db.connection import connect
from wdi.ingest.service import run_import

from agent_watchdog import capture_store, config, readapi


def test_import_refreshes_late_outcomes_and_purges_per_store(tmp_path: Path):
    machine = uuid4()
    data = tmp_path / "watchdog"
    project_dirs = []
    for _ in range(2):
        project = config.Project(id=uuid4(), root=tmp_path)
        directory = data / "projects" / str(project.id)
        directory.mkdir(parents=True)
        capture_store.write_manifest(directory, machine_id=machine, project=project)
        store = capture_store.CaptureStore(directory / capture_store.CAPTURE_FILENAME)
        with store.connect() as db:
            db.execute(
                "INSERT INTO session_env (provider, session_id, captured_at, "
                "harness, files, watchdog) "
                "VALUES ('codex', 's', 't', '{}', '[]', '{}')"
            )
        project_dirs.append(directory)
    settings = Settings(
        machine_id=str(machine),
        db_path=tmp_path / "derived.sqlite",
        sources={
            "claude_code": {"enabled": False, "root": tmp_path / "missing-claude"},
            "codex": {"enabled": False, "root": tmp_path / "missing-codex"},
        },
        watchdog={"data_dirs": [data]},
    )
    run_import(settings)
    run_import(settings)
    with closing(connect(settings.db_path)) as db:
        assert {
            tuple(row)
            for row in db.execute("SELECT machine_id, project_id, store_path FROM session_env")
        } == {
            (str(machine), directory.name, str(directory.resolve())) for directory in project_dirs
        }
    first = capture_store.CaptureStore(project_dirs[0] / capture_store.CAPTURE_FILENAME)
    with first.connect() as db:
        db.execute(
            "INSERT INTO checkout_states VALUES "
            "('c', 'later', 'codex', 's', 'stop', NULL, 'head', NULL, NULL, 2, NULL, 0, NULL, NULL)"
        )
        db.execute("DELETE FROM session_env")
    run_import(settings)
    with closing(connect(settings.db_path)) as db:
        assert [row[0] for row in db.execute("SELECT project_id FROM session_env")] == [
            project_dirs[1].name
        ]
        assert tuple(
            db.execute(
                "SELECT project_id, files_changed, insertions FROM checkout_states"
            ).fetchone()
        ) == (project_dirs[0].name, None, 2)


def test_failed_refresh_keeps_previous_snapshot(tmp_path: Path, monkeypatch):
    from wdi.db.repository import Repository
    from wdi.ingest.environment import import_environment

    project = config.Project(id=uuid4(), root=tmp_path)
    directory = tmp_path / "data" / "projects" / str(project.id)
    directory.mkdir(parents=True)
    capture_store.write_manifest(directory, machine_id=uuid4(), project=project)
    store = capture_store.CaptureStore(directory / capture_store.CAPTURE_FILENAME)
    with store.connect() as db:
        db.execute(
            "INSERT INTO session_env (provider, session_id, captured_at, harness, files, watchdog) "
            "VALUES ('codex', 's', 't', '{}', '[]', '{}')"
        )
    (info,) = readapi.stores(tmp_path / "data")
    with closing(connect(tmp_path / "derived.sqlite")) as db:
        repo = Repository(db)
        import_environment(repo, info)

        def fail(_info):
            raise ValueError("read failure")

        monkeypatch.setattr(readapi, "checkout_states", fail)
        with pytest.raises(ValueError, match="read failure"):
            import_environment(repo, info)
        assert db.execute("SELECT session_id FROM session_env").fetchone()[0] == "s"
