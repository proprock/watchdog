import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from uuid import uuid4

from agent_watchdog.storage import Store

_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_ingestion.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_ingestion", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_benchmark = _MODULE.run_benchmark


def test_benchmark_uses_independent_database_copies_and_reports_counts(tmp_path: Path) -> None:
    project_id = uuid4()
    source_root = tmp_path / "source-project"
    with Store(source_root, project_id):
        pass
    source_db = source_root / "events.sqlite3"
    before = hashlib.sha256(source_db.read_bytes()).digest()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)

    report = run_benchmark(
        source_db,
        checkout,
        tmp_path / "bench",
        events=7,
        profile_events=4,
        measure_control=False,
    )

    assert [result["batch_size"] for result in report["results"]] == [1, 25, 50, 100]
    assert [result["events_inserted"] for result in report["results"]] == [7] * 4
    assert all(result["spool_seconds"] > 0 for result in report["results"])
    assert all(result["quarantined"] == result["discarded"] == 0 for result in report["results"])
    assert all(result["control_latency"] is None for result in report["results"])
    assert all(result["events_profiled"] == 4 for result in report["results"])
    stages = report["results"][0]["stage_profile"]
    assert stages["quota_availability_scan"] > 0
    assert stages["sqlite_insert_execution"] > 0
    assert "file_read_calls_direct_drain_exclusive" in stages
    assert stages["store_open"] > 0
    assert report["results"][0]["stage_counts"]["store_open"] > 0
    assert all(result["duplicates"] == 0 for result in report["results"])
    assert all(result["total_seconds"] > 0 for result in report["results"])
    assert hashlib.sha256(source_db.read_bytes()).digest() == before
    assert str(tmp_path) not in str(report)


def test_control_probe_uses_four_bounded_clients_and_reports_errors(tmp_path, monkeypatch):
    from agent_watchdog.config import Config, Project, UserPaths, save_config

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "state", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(Project(id=uuid4(), root=checkout),)))
    requests = []

    def request(discovery, root):
        assert discovery["token"] and root == checkout
        requests.append(discovery["port"])
        if len(requests) == 1:
            raise TimeoutError("Synthetic timeout")

    monkeypatch.setattr(_MODULE, "_control_request", request)
    with _MODULE._control_probe(paths, checkout, enabled=True) as report:
        _MODULE.time.sleep(0.25)
    assert report["clients"] == 4 and report["budget_ms"] <= 300
    assert report["samples"] + report["errors"] == len(requests) >= 4
    assert report["error_counts"] == {"TimeoutError": 1}
    assert 0 <= report["p50_ms"] <= report["p95_ms"]


def test_control_request_sends_authenticated_legitimate_input_within_budget(tmp_path, monkeypatch):
    class Connection:
        sent = b""
        timeouts = []
        responses = [b'{"schema_version":2,"action":"allow"}', b""]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def sendall(self, data):
            self.sent = data

        def shutdown(self, _):
            pass

        def recv(self, _):
            return self.responses.pop(0)

    connection = Connection()

    def connect(address, *, timeout):
        assert address == ("127.0.0.1", 12345) and timeout <= 0.3
        return connection

    monkeypatch.setattr(_MODULE.socket, "create_connection", connect)
    _MODULE._control_request({"port": 12345, "token": "synthetic-token"}, tmp_path)
    request = json.loads(connection.sent)
    assert request["schema_version"] == 2 and request["token"] == "synthetic-token"
    assert request["input"]["hook_event_name"] == "PreToolUse"
    assert request["input"]["tool_input"] == {"command": "printf benchmark-control"}
    assert all(0 < timeout <= 0.3 for timeout in connection.timeouts)
