"""WD-174: a silent loss of provider coverage must surface in the spool report and doctor."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agent_watchdog import daemon, inspection
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.spool import SCHEMA_VERSION, Spool
from agent_watchdog.storage import DrainResult, Store

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def setup(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    save_config(paths.config, Config(projects=(project,)))
    monkeypatch.setattr(daemon, "alive", lambda _: True)
    monkeypatch.setattr(daemon, "status", lambda _: {"state": "ok"})
    return paths, project


def stale_claude_record() -> dict:
    # What an adapter built before spool schema 2 writes: no record_type.
    return {"schema_version": 1, "provider": "claude", "event_id": str(uuid4())}


def transcript(root: Path, name: str, when: datetime) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text("{}\n")
    os.utime(path, (when.timestamp(), when.timestamp()))


def store_event(paths, project, provider: str, when: datetime) -> None:
    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                provider=provider,
                project_id=project.id,
                kind="turn.start",
                source="hook",
                received_at=when,
            )
        )


def test_schema_mismatch_is_reported_by_version_not_only_as_invalid(tmp_path):
    queue = Spool(tmp_path)
    for _ in range(2):
        queue.publish_record(stale_claude_record())
    result = DrainResult()
    assert list(queue.records(result)) == []
    report = queue.schema_mismatch()
    assert report is not None
    assert report["expected"] == SCHEMA_VERSION
    assert report["seen"] == {"1": 2}
    assert datetime.fromisoformat(report["last_at"]).tzinfo is not None


def test_a_matching_schema_leaves_no_mismatch_report(tmp_path):
    queue = Spool(tmp_path)
    queue.publish_record({"schema_version": SCHEMA_VERSION, "record_type": "hook"})
    assert len(list(queue.records(DrainResult()))) == 1
    assert queue.schema_mismatch() is None


def test_a_malformed_record_is_not_a_schema_mismatch(tmp_path):
    queue = Spool(tmp_path)
    queue.directory.mkdir()
    (queue.directory / "bad.json").write_bytes(b"not json")
    list(queue.records(DrainResult()))
    assert queue.schema_mismatch() is None


def test_doctor_fails_on_a_recent_schema_mismatch(tmp_path, monkeypatch):
    paths, _ = setup(tmp_path, monkeypatch)
    queue = Spool(paths.data)
    queue.publish_record(stale_claude_record())
    list(queue.records(DrainResult()))
    report = inspection.doctor(paths, now=datetime.now(UTC), transcript_roots={})
    assert report["ok"] is False
    assert report["adapter"]["schema_mismatch"]["seen"] == {"1": 1}
    assert "adapter_schema_mismatch" in {item["code"] for item in report["warnings"]}


def test_doctor_reports_effective_retention_and_disabled_quota(tmp_path, monkeypatch):
    paths, _ = setup(tmp_path, monkeypatch)
    report = inspection.doctor(paths, now=datetime.now(UTC), transcript_roots={})
    assert report["projects"][0]["limits"] == {
        "content_days": 0,
        "metrics_days": 0,
        "quota": "disabled",
    }


def test_doctor_reports_raw_capture_without_initializing_it(tmp_path, monkeypatch):
    from agent_watchdog.capture import capture
    from agent_watchdog.capture_store import CAPTURE_FILENAME

    paths, project = setup(tmp_path, monkeypatch)
    root = paths.project_data(project.id)
    assert inspection.doctor(paths, transcript_roots={})["projects"][0]["capture"]["streams"] == 0
    assert not (root / CAPTURE_FILENAME).exists()
    source = tmp_path / "session.jsonl"
    source.write_bytes(b"payload\n")
    with Store(root, project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                kind="turn.start",
                source="hook",
                session_id="session",
                payload={"claude": {"transcript_path": str(source)}},
            )
        )
        capture(store)
    report = inspection.doctor(paths, transcript_roots={})
    assert report["projects"][0]["capture"] == {
        "streams": 1,
        "open": 1,
        "missing": 0,
        "raw_bytes": 8,
        "stored_bytes": 7,
        "skipped_lines": 0,
        "last_errors": [],
    }


def test_doctor_ignores_a_mismatch_older_than_the_window(tmp_path, monkeypatch):
    paths, _ = setup(tmp_path, monkeypatch)
    queue = Spool(paths.data)
    queue.publish_record(stale_claude_record())
    list(queue.records(DrainResult()))
    later = datetime.now(UTC) + timedelta(hours=25)
    report = inspection.doctor(paths, now=later, transcript_roots={})
    assert report["adapter"]["schema_mismatch"] is not None
    assert "adapter_schema_mismatch" not in {item["code"] for item in report["warnings"]}
    assert report["ok"] is True


def test_doctor_warns_when_transcripts_are_active_but_no_event_is_stored(tmp_path, monkeypatch):
    paths, project = setup(tmp_path, monkeypatch)
    store_event(paths, project, "claude", NOW - timedelta(days=3))
    store_event(paths, project, "codex", NOW - timedelta(hours=1))
    roots = {"claude": tmp_path / "claude", "codex": tmp_path / "codex"}
    transcript(roots["claude"] / "project", "a.jsonl", NOW - timedelta(hours=2))
    transcript(roots["codex"], "b.jsonl", NOW - timedelta(hours=2))
    report = inspection.doctor(paths, now=NOW, transcript_roots=roots)
    warnings = {(item["code"], item.get("provider")) for item in report["warnings"]}
    assert ("provider_transcripts_without_events", "claude") in warnings
    assert ("provider_transcripts_without_events", "codex") not in warnings
    assert report["coverage"]["claude"]["transcript_activity"] is True
    assert report["coverage"]["claude"]["last_event_at"] is not None


def test_doctor_does_not_warn_for_an_idle_provider(tmp_path, monkeypatch):
    paths, project = setup(tmp_path, monkeypatch)
    store_event(paths, project, "codex", NOW - timedelta(hours=1))
    roots = {"claude": tmp_path / "claude"}
    transcript(roots["claude"] / "project", "a.jsonl", NOW - timedelta(days=9))
    report = inspection.doctor(paths, now=NOW, transcript_roots=roots)
    assert {item["code"] for item in report["warnings"]} == {"scan_disabled"}
    assert report["coverage"]["claude"]["transcript_activity"] is False


def test_a_missing_transcript_root_is_unknown_not_a_warning(tmp_path, monkeypatch):
    paths, _ = setup(tmp_path, monkeypatch)
    roots = {"claude": tmp_path / "absent"}
    report = inspection.doctor(paths, now=NOW, transcript_roots=roots)
    assert {item["code"] for item in report["warnings"]} == {"scan_disabled"}
    assert report["coverage"]["claude"]["transcript_activity"] is None


def test_report_is_json_serializable(tmp_path, monkeypatch):
    paths, _ = setup(tmp_path, monkeypatch)
    json.dumps(inspection.doctor(paths, now=NOW, transcript_roots={}))
