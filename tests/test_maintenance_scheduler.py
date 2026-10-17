"""One cleanup allowance is shared fairly before admitting more queue work."""

from contextlib import ExitStack
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_watchdog import daemon, resources, transcripts
from agent_watchdog.config import Config, Project, UserPaths
from agent_watchdog.storage import MaintenanceBudget, MaintenanceResult, MaintenanceState


def test_projects_take_turns_before_ingestion_can_consume_cleanup_budget(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    projects = tuple(Project(id=uuid4(), root=tmp_path / str(index)) for index in range(2))
    config = Config(projects=projects)
    units, drains, closed = [], [], []
    tick = 0

    class FakeStore:
        def __init__(self, root, project_id, **kwargs):
            self.project_id = project_id
            self.maintenance_state = MaintenanceState()
            self.maintenance_budget: MaintenanceBudget | None = None
            self.touched = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(self.project_id)

        def maintain(self):
            assert self.maintenance_budget is not None
            if self.maintenance_budget.remaining > 0:
                units.append((tick, self.project_id))
                self.maintenance_budget.remaining = 0
            return MaintenanceResult(pending=True)

    def drain(*args, stores, maintenance_budget, **kwargs):
        assert len(stores) == 2
        assert maintenance_budget.remaining == 0
        assert all(store.maintenance_budget is maintenance_budget for store in stores.values())
        drains.append(tick)
        return False

    def sleep(_):
        nonlocal tick
        tick += 1
        if tick == 2:
            raise StopIteration

    sessions = daemon._SessionTracker()
    channel = daemon._Channel(paths)
    monkeypatch.setattr(sessions, "knows", lambda _: True)
    monkeypatch.setattr(sessions, "load", lambda *args: None)
    monkeypatch.setattr(sessions, "absorb", lambda *args: None)
    monkeypatch.setattr(sessions, "refresh", lambda *args, **kwargs: (0, []))
    monkeypatch.setattr(channel, "sync", lambda _: None)
    enrichment = SimpleNamespace(active_failures=(), inert=False)
    monkeypatch.setattr(daemon, "Store", FakeStore)
    monkeypatch.setattr(daemon._config_cache, "load", lambda _: config)
    monkeypatch.setattr(daemon, "desired", lambda _: {"paused": False})
    monkeypatch.setattr(daemon, "_drain_controls", lambda *args: False)
    monkeypatch.setattr(daemon, "_drain_spool", drain)
    monkeypatch.setattr(daemon, "_queue_status", lambda *args: {"spool": {"files": 0}})
    monkeypatch.setattr(daemon, "atomic_write", lambda *args: None)
    monkeypatch.setattr(daemon, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon.daemon_lifecycle, "record_ready", lambda *args: None)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    monkeypatch.setattr(transcripts, "enrich", lambda *args, **kwargs: enrichment)
    monkeypatch.setattr(resources, "available", lambda *args: 10**9)
    with ExitStack() as owner, pytest.raises(StopIteration):
        daemon._poll(paths, owner, sessions, channel)
    assert units == [(0, projects[0].id), (1, projects[1].id)]
    assert drains == [0, 1]
    assert len(closed) == 4
