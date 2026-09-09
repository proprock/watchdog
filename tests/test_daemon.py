import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.config import Config, Limits, Overrides, UserPaths, load_config, save_config
from agent_watchdog.daemon import (
    _drain_spool,
    _record_enrichment_failures,
    enqueue,
    launch,
    mutate_registry,
    start,
    status,
    stop,
    write_spool_limits,
)
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry
from agent_watchdog.resources import losses
from agent_watchdog.storage import Inbox, Store, WriterBusy, writer_lock
from agent_watchdog.transcripts import FAILURE_CODES


def spool_record(cwd, **input_fields):
    return {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "received_at": datetime.now(UTC).isoformat(),
        "provider": "codex",
        "cwd": str(cwd),
        "input": input_fields,
    }


def write_spool_record(paths, record):
    directory = paths.data / "spool"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{uuid4().hex}.json").write_text(json.dumps(record), encoding="utf-8")


def drain_inbox(paths, project, config):
    root = paths.project_data(project.id)
    limits = project.overrides.apply(config.defaults)
    with Store(root, project.id, limits=limits) as store:
        return Inbox(root, limits=limits).drain(store)


def test_spool_record_is_resolved_built_and_admitted(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(
        root,
        hook_event_name="UserPromptSubmit",
        session_id="s1",
        prompt="explain code; password=private-value",
    )
    write_spool_record(paths, record)
    inbox = paths.project_data(project.id) / "inbox"

    assert _drain_spool(paths, config) is True
    assert not list((paths.data / "spool").glob("*.json"))
    incoming = list(inbox.glob("*.json"))
    assert len(incoming) == 1
    envelope = Envelope.model_validate_json(incoming[0].read_bytes())
    assert str(envelope.event_id) == record["event_id"]
    assert envelope.kind == "turn.start" and envelope.session_id == "s1"
    resolved = Registry(config).resolve(root)
    assert resolved is not None and envelope.checkout_id == resolved.checkout_id
    assert b"private-value" in incoming[0].read_bytes()
    assert "explain code" in envelope.model_dump_json()


def test_spool_drain_preserves_enabled_delivery_trace_and_disabled_omits_it(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(root, hook_event_name="Stop", session_id="s1")
    record["delivery"] = {
        "adapter_started_at": "2026-09-08T00:00:00+00:00",
        "spool_enqueued_at": "2026-09-08T00:00:01+00:00",
        "spool_occupancy": {"files": 0, "bytes": 0},
    }
    write_spool_record(paths, record)

    assert _drain_spool(paths, config)
    incoming = next((paths.project_data(project.id) / "inbox").glob("*.json"))
    traced = Envelope.model_validate_json(incoming.read_bytes())
    assert set(traced.delivery) == {
        "adapter_started_at",
        "spool_enqueued_at",
        "spool_occupancy",
        "spool_drained_at",
        "inbox_enqueued_at",
        "inbox_occupancy",
    }
    with Store(paths.project_data(project.id), project.id) as store:
        assert (
            Inbox(paths.project_data(project.id)).drain(store, pipeline_telemetry=True).inserted
            == 1
        )
        stored = store.events()[0]
    assert {"inbox_drained_at", "sqlite_write_started_at"} <= set(stored.delivery)

    disabled = config.model_copy(update={"pipeline_telemetry": False})
    write_spool_record(paths, record | {"event_id": str(uuid4())})
    assert _drain_spool(paths, disabled)
    incoming = next((paths.project_data(project.id) / "inbox").glob("*.json"))
    assert Envelope.model_validate_json(incoming.read_bytes()).delivery == {}


def test_spool_drain_records_a_debounced_content_free_diff_fingerprint(
    paths, tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    monkeypatch.setattr(
        "agent_watchdog.analysis.git_diff_fingerprint", lambda checkout: ("a" * 64, 123)
    )
    write_spool_record(paths, spool_record(root, hook_event_name="Stop", session_id="s1"))

    assert _drain_spool(paths, config) is True
    with Store(paths.project_data(project.id), project.id) as store:
        rows = store.connection.execute(
            "SELECT fingerprint, byte_count FROM diff_snapshots"
        ).fetchall()
    assert rows == [("a" * 64, 123)]


def test_spool_unregistered_cwd_is_discarded_without_loss(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(log_level="DEBUG")})
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    write_spool_record(paths, spool_record(foreign, hook_event_name="Stop"))

    assert _drain_spool(paths, config) is True
    assert not list((paths.data / "spool").glob("*.json"))
    assert not list(paths.data.glob("projects/*/inbox/*.json"))
    assert losses(paths.data)["invalid"] == 0
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=unregistered" in body
    assert str(foreign) not in body


def test_invalid_spool_log_uses_a_fixed_reason_without_record_content(paths):
    config = Config(defaults=Limits(log_level="DEBUG"))
    secret = "prompt=private-value"
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text(secret, encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=discarded reason=invalid" in body
    assert secret not in body


def test_invalid_spool_log_names_the_error_category(paths):
    config = Config(defaults=Limits(log_level="DEBUG"))
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text("not json", encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "component=daemon event=spool decision=discarded reason=invalid error_type=jsondecodeerror"
        in body
    )


def test_log_detail_opt_in_adds_the_error_string_to_spool_failures(paths):
    config = Config(defaults=Limits(log_level="DEBUG", log_detail=True))
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text("not json", encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "reason=invalid error_type=jsondecodeerror" in body
    assert ' detail="' in body and "Expecting value" in body


def test_oversized_spool_record_is_discarded_with_a_size_reason(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(log_level="DEBUG")})
    record = spool_record(root, hook_event_name="Stop", filler="x" * (1024**2 + 128))
    write_spool_record(paths, record)

    assert _drain_spool(paths, config) is True

    assert not list((paths.data / "spool").glob("*.json"))
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=discarded reason=oversized" in body


def test_spool_trusted_git_project_is_auto_added_and_admitted(paths, tmp_path):
    trusted = tmp_path / "repos"
    repository = trusted / "new-project"
    repository.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    config = Config(auto_add_projects=True, trusted_projects_dir=trusted)
    save_config(paths.config, config)
    write_spool_record(paths, spool_record(repository, hook_event_name="Stop"))

    assert _drain_spool(paths, config) is True
    persisted = load_config(paths.config)
    assert len(persisted.projects) == 1
    project = persisted.projects[0]
    assert project.root == repository.resolve()
    assert len(list((paths.project_data(project.id) / "inbox").glob("*.json"))) == 1


def test_spool_drain_enforces_the_project_payload_limit(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(payload_bytes=4096)})
    project = config.projects[0]
    # ensure_ascii expansion of these code points pushes the canonical envelope
    # past 4096 bytes; the drain, not the adapter, rejects it here.
    write_spool_record(
        paths, spool_record(root, hook_event_name="UserPromptSubmit", prompt="界" * 600)
    )

    assert _drain_spool(paths, config) is True
    assert not list((paths.project_data(project.id) / "inbox").glob("*.json"))
    assert losses(paths.project_data(project.id))["payload"] == 1


def test_spool_redrain_is_idempotent(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(root, hook_event_name="Stop")
    write_spool_record(paths, record)
    assert _drain_spool(paths, config) is True
    # A second identical record (a replayed spool file) must not duplicate the event.
    write_spool_record(paths, record)
    assert _drain_spool(paths, config) is True
    result = drain_inbox(paths, project, config)
    assert result.inserted == 1 and result.duplicates == 1
    with Store(paths.project_data(project.id), project.id) as store:
        assert [str(item.event_id) for item in store.events()] == [record["event_id"]]


def test_write_spool_limits_uses_largest_project_payload(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    write_spool_limits(paths, config)
    published = json.loads((paths.data / "spool" / "limits.json").read_text())
    assert published["schema_version"] == 1
    assert published["payload_bytes"] == config.defaults.payload_bytes
    assert published["spool_files"] > 0 and published["spool_bytes"] > 0


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Timed out waiting for daemon")


@pytest.fixture
def paths(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    yield paths
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])


def test_concurrent_start_pause_and_explicit_resume(paths):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: start(paths), range(4)))
    wait_for(lambda: status(paths)["state"] == "running")
    instance = status(paths)["instance_id"]
    start(paths)
    assert status(paths)["instance_id"] == instance
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    assert status(paths)["state"] == "paused"
    assert not start(paths, explicit=False)
    assert not enqueue(
        paths, Envelope(project_id=uuid4(), provider="codex", kind="unknown", source="hook")
    )
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    assert status(paths)["instance_id"] != instance


def test_registry_mutations_do_not_lose_updates(paths, tmp_path):
    roots = [tmp_path / str(index) for index in range(4)]
    for root in roots:
        root.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda root: mutate_registry(paths, lambda registry: registry.add(root)), roots
            )
        )
    assert len(load_config(paths.config).projects) == 4


def test_daemon_drains_only_registered_events(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    event = Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    assert not enqueue(paths, event.model_copy(update={"project_id": uuid4()}))
    assert enqueue(paths, event)
    inbox = paths.project_data(project.id) / "inbox"
    wait_for(lambda: not list(inbox.glob("*.json")))
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    with Store(paths.project_data(project.id), project.id) as store:
        assert [item.event_id for item in store.events()] == [event.event_id]


def test_stale_pid_is_not_treated_as_ownership(paths):
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(
        json.dumps({"pid": 1, "heartbeat": time.time(), "instance_id": "stale"})
    )
    assert status(paths)["state"] == "unavailable"
    stop(paths)
    assert status(paths)["state"] == "paused"


def test_crash_releases_lock_and_hook_recovers(paths):
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("daemon_crash_probe.py")),
            str(paths.config.parent),
        ]
    )
    try:
        wait_for(lambda: status(paths)["state"] == "running")
        instance = status(paths)["instance_id"]
        (paths.config.parent / "crash").touch()
        assert process.wait(timeout=10) == 91
        assert status(paths)["state"] == "unavailable"
        assert start(paths, explicit=False)
        wait_for(lambda: status(paths)["state"] == "running")
        assert status(paths)["instance_id"] != instance
    finally:
        if process.poll() is None:
            (paths.config.parent / "crash").touch()
            process.wait(timeout=10)


def test_stop_followed_immediately_by_start_does_not_lose_restart(paths):
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    for _ in range(4):
        stop(paths)
        start(paths)
        wait_for(lambda: status(paths).get("acknowledged_request") == status(paths)["request_id"])
        assert status(paths)["alive"]


def test_bad_project_is_degraded_and_recovers(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    data = paths.project_data(project.id)
    data.mkdir(parents=True)
    database = data / "events.sqlite3"
    database.write_bytes(b"not a database")
    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    assert status(paths)["state"] == "degraded"
    database.unlink()
    wait_for(lambda: status(paths)["state"] == "running")


def test_enrichment_failure_logs_are_allowlisted_debounced_and_content_free(paths):
    project_id = uuid4()
    config = Config(defaults=Limits(log_level="DEBUG"))
    logged: set[tuple[str, str]] = set()
    secret = "C:/private/rollout.jsonl prompt=private-value tool output"

    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        tuple(sorted(FAILURE_CODES)),
        logged,
        detail=secret,
    )
    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        tuple(sorted(FAILURE_CODES)),
        logged,
        detail=secret,
    )

    lines = (paths.data / "watchdog.log").read_text(encoding="ascii").splitlines()
    assert len(lines) == len(FAILURE_CODES)
    assert {line.split("error_type=", 1)[1].split()[0] for line in lines} == FAILURE_CODES
    assert all("component=daemon event=enrichment decision=unavailable" in line for line in lines)
    assert all(f"project_id={project_id}" in line for line in lines)
    assert secret not in "\n".join(lines) and "detail=" not in "\n".join(lines)

    assert not _record_enrichment_failures(paths, config, project_id, (), logged)
    assert not logged


def test_enrichment_log_detail_uses_the_global_opt_in_contract(paths):
    config = Config(defaults=Limits(log_level="DEBUG", log_detail=True))
    project_id = uuid4()
    logged: set[tuple[str, str]] = set()
    secret = "sk-proj-" + "a" * 40
    detail = f"C:/private/rollout.jsonl Bearer {secret} " + "x" * 400

    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        ("transcript_storage_unavailable",),
        logged,
        detail=detail,
    )

    line = (paths.data / "watchdog.log").read_text(encoding="ascii").strip()
    assert "error_type=transcript_storage_unavailable" in line
    assert secret not in line and ' detail="' in line and "C:/private/rollout.jsonl" in line


def test_missing_transcript_degrades_the_daemon_until_the_source_recovers(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    missing = tmp_path / "private-rollout.jsonl"
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    write_spool_record(
        paths,
        spool_record(
            root,
            hook_event_name="Stop",
            session_id="session-1",
            transcript_path=str(missing),
        ),
    )

    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    report = status(paths)
    assert report["state"] == "degraded"
    assert report["transcript_failures"] == {str(project.id): ["transcript_unreadable"]}
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "event=enrichment decision=unavailable "
        f"error_type=transcript_unreadable project_id={project.id}" in body
    )
    assert str(missing) not in body

    missing.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "cli_version": "0.153.4"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    wait_for(lambda: status(paths)["state"] == "running")


def test_stale_transcript_failure_does_not_degrade_daemon(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    updated = config.model_copy(
        update={
            "projects": (
                project.model_copy(update={"overrides": Overrides(transcript_failure_minutes=1)}),
            )
        }
    )
    save_config(paths.config, updated)
    record = spool_record(
        root,
        hook_event_name="Stop",
        session_id="session-1",
        transcript_path=str(tmp_path / "missing.jsonl"),
    )
    record["received_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    write_spool_record(paths, record)

    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")

    with Store(paths.project_data(project.id), project.id) as store:
        sources = store.transcript_sources()
    assert sources[0]["last_error"] == "transcript_unreadable"
    assert status(paths)["transcript_failures"] == {}


def test_inert_enrichment_is_logged_and_degrades_until_it_resolves(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    transcript = tmp_path / "session.jsonl"
    skipped = {
        "type": "assistant",
        "sessionId": "session-1",
        "requestId": "req_child",
        "isSidechain": True,
        "timestamp": "2026-09-09T04:58:34.955Z",
        "version": "2.1.260",
        "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 9}},
    }
    transcript.write_text(json.dumps(skipped) + "\n", encoding="utf-8")
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    write_spool_record(
        paths,
        spool_record(
            root,
            hook_event_name="Stop",
            session_id="session-1",
            transcript_path=str(transcript),
        )
        | {"provider": "claude"},
    )

    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    assert status(paths)["state"] == "degraded"
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "event=enrichment decision=inert reason=usage_seen_unstored "
        f"project_id={project.id}" in body
    )
    assert str(transcript) not in body
    assert body.count("decision=inert") == 1  # debounced

    # An accepted (non-sidechain) line clears the inert state.
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(skipped | {"requestId": "req_real", "isSidechain": False}) + "\n")
    wait_for(lambda: str(project.id) not in status(paths).get("errors", []))


@pytest.mark.parametrize("content", ['{"schema_version": 99}', "{}"])
def test_corrupt_control_is_never_overwritten(paths, content):
    paths.data.mkdir(parents=True)
    control = paths.data / "control.json"
    control.write_text(content)
    with pytest.raises(ValueError):
        start(paths)
    with pytest.raises(ValueError):
        stop(paths)
    assert control.read_text() == content
    control.unlink()


def test_stale_heartbeat_under_a_held_lock_is_degraded(paths):
    paths.data.mkdir(parents=True)
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(json.dumps({"heartbeat": 1, "pid": 1}))
    with writer_lock(paths.data / "daemon.lock"):
        assert status(paths)["state"] == "degraded"
        assert status(paths)["alive"]
    assert status(paths)["state"] == "unavailable"


def test_status_reports_current_queue_occupancy(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    Inbox(paths.project_data(project.id)).publish(
        Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    )

    queues = status(paths)["queues"]
    assert queues["spool"]["files"] == 0
    inbox = queues["projects"][str(project.id)]
    assert inbox["files"] == 1 and inbox["bytes"] > 0
    assert inbox["byte_limit"] == config.defaults.inbox_bytes
    assert inbox["file_limit"] is None


def test_hook_start_contention_is_bounded(paths):
    paths.data.mkdir(parents=True)
    with writer_lock(paths.data / "control.lock"):
        before = time.monotonic()
        with pytest.raises(WriterBusy):
            start(paths, explicit=False)
        assert time.monotonic() - before < 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation flags are platform-specific")
def test_daemon_launch_uses_an_invisible_process_group(paths, monkeypatch):
    save_config(paths.config, Config())
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        calls.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr("agent_watchdog.daemon.subprocess.Popen", fake_popen)
    launch(paths)

    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not flags & subprocess.DETACHED_PROCESS


def test_independent_cli_startup_contenders_and_pause(paths):
    command = [sys.executable, "-m", "agent_watchdog", "--home", str(paths.config.parent), "daemon"]
    processes = [
        subprocess.Popen(
            command + ["start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    try:
        reports = []
        for process in processes:
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, (output, errors)
            reports.append(json.loads(output))
        assert len({report["instance_id"] for report in reports}) == 1
        result = subprocess.run(command + ["pause"], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0
        assert json.loads(result.stdout)["alive"] is False
    finally:
        stop(paths)
        for process in processes:
            process.communicate(timeout=15)
