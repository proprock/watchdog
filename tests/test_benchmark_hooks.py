import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_hooks.py"
    spec = importlib.util.spec_from_file_location("benchmark_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_waits_for_live_pid_after_status_invalidation(benchmark, monkeypatch):
    ready = {"state": "running", "alive": True, "pid": 123}
    reports = iter([{}, {"state": "starting", "alive": True}, ready])
    monkeypatch.setattr(
        benchmark, "run", lambda _: SimpleNamespace(stdout=json.dumps(next(reports)))
    )
    monkeypatch.setattr(benchmark.time, "sleep", lambda _: None)
    assert benchmark.wait_for_daemon(["watchdog"]) == ready


def test_benchmark_rejects_stale_daemon_and_bounds_readiness(benchmark, monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "run",
        lambda _: SimpleNamespace(stdout='{"state":"unavailable","alive":false,"pid":123}'),
    )
    clock = iter([0, 16])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not become ready"):
        benchmark.wait_for_daemon(["watchdog"])
