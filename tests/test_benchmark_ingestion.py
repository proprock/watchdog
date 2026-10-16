import hashlib
import importlib.util
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

    report = run_benchmark(source_db, checkout, tmp_path / "bench", events=105, profile_events=4)

    assert [result["batch_size"] for result in report["results"]] == [1, 25]
    assert [result["events_inserted"] for result in report["results"]] == [105, 105]
    assert all(result["spool_seconds"] > 0 for result in report["results"])
    assert all(result["inbox_seconds"] > 0 for result in report["results"])
    assert all(result["events_profiled"] == 4 for result in report["results"])
    stages = report["results"][0]["stage_profile"]
    assert stages["quota_availability_scan"] > 0
    assert stages["sqlite_insert_execution"] > 0
    assert "file_read_calls_spool_phase_exclusive" in stages
    assert "file_read_calls_inbox_phase_exclusive" in stages
    assert all(result["duplicates"] == 0 for result in report["results"])
    assert all(result["total_seconds"] > 0 for result in report["results"])
    assert hashlib.sha256(source_db.read_bytes()).digest() == before
    assert str(tmp_path) not in str(report)
