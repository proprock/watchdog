import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_watchdog.analysis import analyze_telemetry
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store


def envelope(project, received_at, delivery):
    return Envelope(
        provider="codex",
        project_id=project.id,
        session_id="session",
        kind="turn.start",
        source="hook",
        received_at=received_at,
        delivery=delivery,
    )


def delivery(start, seconds, occupancy, *, legacy=False):
    result = {
        "adapter_started_at": start.isoformat(),
        "spool_enqueued_at": (start + timedelta(seconds=seconds)).isoformat(),
        "spool_drained_at": (start + timedelta(seconds=seconds + 1)).isoformat(),
        "inbox_enqueued_at": (start + timedelta(seconds=seconds + 2)).isoformat(),
        "inbox_drained_at": (start + timedelta(seconds=seconds + 3)).isoformat(),
        "sqlite_write_started_at": (start + timedelta(seconds=seconds + 4)).isoformat(),
    }
    suffix = "queue" if legacy else "occupancy"
    result[f"spool_{suffix}"] = {"files": occupancy, "bytes": occupancy * 10}
    result[f"inbox_{suffix}"] = {"files": occupancy + 1, "bytes": occupancy * 20}
    return result


def invoke(monkeypatch, capsys, home, *args):
    monkeypatch.setattr(sys, "argv", ["agent-watchdog", "--home", str(home), *args])
    code = main()
    return code, json.loads(capsys.readouterr().out)


def test_telemetry_uses_nearest_rank_and_counts_bad_pipeline_observations(tmp_path):
    project = Project(id=uuid4(), root=tmp_path)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    received = (
        start + timedelta(minutes=10),
        start + timedelta(minutes=20),
        start + timedelta(hours=1),
        start + timedelta(hours=1, minutes=10),
        start + timedelta(hours=2),
    )
    events = [
        envelope(
            project,
            observed,
            delivery(start + timedelta(hours=index), seconds, seconds, legacy=index == 4),
        )
        for index, (observed, seconds) in enumerate(zip(received, (1, 2, 3, 4, 100), strict=True))
    ]
    events[0] = events[0].model_copy(update={"payload": {"codex": {"content": "DO_NOT_LEAK"}}})
    events.append(
        envelope(
            project,
            start + timedelta(hours=3),
            {
                "adapter_started_at": "invalid",
                "spool_enqueued_at": (start + timedelta(seconds=2)).isoformat(),
                "spool_drained_at": (start + timedelta(seconds=1)).isoformat(),
                "spool_occupancy": {"files": True, "bytes": -1},
            },
        )
    )

    result = analyze_telemetry(events)

    assert result["trace_coverage"]["events"] == 6
    assert result["trace_coverage"]["events_with_delivery"] == 6
    assert result["trace_coverage"]["complete_traces"] == 5
    assert result["trace_coverage"]["timestamp_fields"]["adapter_started_at"] == {
        "observed": 5,
        "missing": 0,
        "invalid": 1,
    }
    adapter = result["stage_durations_seconds"]["adapter_to_spool"]
    assert (adapter["observed_count"], adapter["invalid_count"]) == (5, 1)
    assert (adapter["p50"], adapter["p95"], adapter["p99"], adapter["max"]) == (
        3.0,
        100.0,
        100.0,
        100.0,
    )
    assert result["stage_durations_seconds"]["spool_queue"]["out_of_order_count"] == 1
    assert result["stage_durations_seconds"]["inbox_queue"]["missing_count"] == 1
    assert result["occupancy"]["spool"]["observed_count"] == 5
    assert result["occupancy"]["spool"]["invalid_count"] == 1
    assert result["occupancy"]["spool"]["files"] == {
        "p50": 3,
        "p95": 100,
        "p99": 100,
        "max": 100,
    }
    assert result["throughput"]["peak_hourly_rate"] == 2
    assert result["throughput"]["peak_hour_started_at"] == start.isoformat()
    assert "DO_NOT_LEAK" not in json.dumps(result)


def test_telemetry_cli_filters_since_and_is_read_only(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(pipeline_telemetry=True, projects=(project,)))
    root = tmp_path / "data" / "projects" / str(project.id)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    with Store(root, project.id) as store:
        store.put(envelope(project, start, delivery(start, 1, 1)))
        store.put(
            envelope(
                project,
                start + timedelta(hours=1),
                delivery(start + timedelta(hours=1), 2, 2),
            )
        )
    before = (root / "events.sqlite3").read_bytes()

    code, result = invoke(
        monkeypatch,
        capsys,
        tmp_path,
        "telemetry",
        "--project",
        tmp_path.name,
        "--since",
        (start + timedelta(minutes=30)).isoformat(),
    )

    assert code == 0
    assert result["project"] == tmp_path.name
    assert result["telemetry_disabled"] is False
    assert result["trace_coverage"]["events"] == 1
    assert result["stage_durations_seconds"]["adapter_to_spool"]["p50"] == 2.0
    assert result["losses"]["current"]["project"] == {
        "payload": 0,
        "quota": 0,
        "invalid": 0,
        "io": 0,
        "busy": 0,
    }
    assert result["losses"]["deltas"] is None
    assert (root / "events.sqlite3").read_bytes() == before


def test_disabled_telemetry_still_validates_project_selection(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(pipeline_telemetry=False, projects=(project,)))

    code, result = invoke(monkeypatch, capsys, tmp_path, "telemetry", "--project", tmp_path.name)
    assert code == 0
    assert result["telemetry_disabled"] is True

    code, result = invoke(monkeypatch, capsys, tmp_path, "telemetry", "--project", "missing")
    assert code == 1
    assert result["error"] == "StorageError"
