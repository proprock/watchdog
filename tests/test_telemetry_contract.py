import io
import json
from datetime import UTC, datetime
from uuid import uuid4

from agent_watchdog.config import Config, Limits, Project, UserPaths, save_config
from agent_watchdog.daemon import _drain_spool
from agent_watchdog.events import Envelope
from agent_watchdog.hooks import observe
from agent_watchdog.inspection import export_sessions, report
from agent_watchdog.storage import Inbox, Store


def configured(tmp_path, *, capture_content=True):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=tmp_path)
    config = Config(defaults=Limits(capture_content=capture_content), projects=(project,))
    save_config(paths.config, config)
    return paths, project, config


def test_python_adapter_retains_redacted_unknown_telemetry_and_warns(tmp_path, monkeypatch):
    paths, project, _ = configured(tmp_path)
    captured = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda _, event: captured.append(event) or True
    )
    secret = "private-value"
    observe(
        paths,
        io.BytesIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "hook_event_name": "FutureEvent",
                    "session_id": "session-1",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                    "input_tokens": 123,
                    "future_metric": {"password": secret, "value": 7},
                    "api_key": secret,
                }
            ).encode()
        ),
    )

    event = captured[0]
    payload = event.payload["codex"]
    assert event.kind == "unknown"
    assert payload["metadata"]["hook_event_name"] == "FutureEvent"
    assert payload["metadata"]["model"] == "gpt-test"
    assert payload["metadata"]["reasoning_effort"] == "high"
    assert payload["metadata"]["input_tokens"] == 123
    assert payload["metadata"]["future_metric"] == {"password": "[REDACTED]", "value": 7}
    assert payload["metadata"]["api_key"] == "[REDACTED]"
    assert payload["unknown_fields"] == ["api_key", "future_metric"]
    assert event.availability["input.future_metric"] == "observed"
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "level=WARNING component=hook event=observe decision=received reason=unknown_field" in body
    )
    assert "provider=codex field=future_metric" in body
    assert secret not in body


def test_current_harness_fields_are_inventoried_not_warned(tmp_path, monkeypatch):
    paths, project, _ = configured(tmp_path)
    captured = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda _, event: captured.append(event) or True
    )
    observe(
        paths,
        io.BytesIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "permission_mode": "acceptEdits",
                    "prompt_id": "prompt-7",
                    "scratchpad_dir": str(tmp_path / "scratch"),
                    "effort": "high",
                    "background_tasks": ["t1"],
                    "session_crons": [],
                    "session_title": "demo",
                    "future_metric": {"value": 7},
                }
            ).encode()
        ),
        "claude",
    )

    event = captured[0]
    payload = event.payload["claude"]
    assert payload["unknown_fields"] == ["future_metric"]
    assert payload["metadata"]["permission_mode"] == "acceptEdits"
    assert payload["metadata"]["prompt_id"] == "prompt-7"
    assert payload["metadata"]["effort"] == "high"
    assert event.availability["input.permission_mode"] == "observed"
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "provider=claude field=future_metric" in body
    assert "field=permission_mode" not in body
    assert "field=prompt_id" not in body
    assert "field=session_crons" not in body


def test_unknown_metadata_survives_capture_content_opt_out(tmp_path, monkeypatch):
    paths, _, _ = configured(tmp_path, capture_content=False)
    captured = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda _, event: captured.append(event) or True
    )
    observe(
        paths,
        io.BytesIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "hook_event_name": "Stop",
                    "prompt": "do not capture this",
                    "future_metric": "retain this",
                }
            ).encode()
        ),
    )

    payload = captured[0].payload["codex"]
    assert payload["content"] == "omitted"
    assert payload["metadata"]["future_metric"] == "retain this"
    assert captured[0].availability["input.prompt"] == "unavailable"


def test_daemon_spool_retains_telemetry_reports_coverage_and_exports(tmp_path):
    paths, project, config = configured(tmp_path)
    record = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "received_at": datetime.now(UTC).isoformat(),
        "provider": "codex",
        "cwd": str(tmp_path),
        "input": {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "future_metric": {"enabled": True},
        },
    }
    spool = paths.data / "spool"
    spool.mkdir(parents=True)
    (spool / "record.json").write_text(json.dumps(record), encoding="utf-8")

    assert _drain_spool(paths, config)
    inbox = Inbox(paths.project_data(project.id))
    with Store(paths.project_data(project.id), project.id) as store:
        assert inbox.drain(store).inserted == 1
        stored = store.events()[0]
    provider_payload = stored.payload["codex"]
    assert isinstance(provider_payload, dict)
    metadata = provider_payload["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["model"] == "gpt-test"
    assert provider_payload["unknown_fields"] == ["future_metric"]

    result = report(paths, project, session_id="session-1", provider="codex")
    telemetry = result["metrics"]["telemetry"]
    assert telemetry["observed_fields"]["model"] == 1
    assert telemetry["unknown_fields"] == {"future_metric": 1}

    output = tmp_path / "export"
    export_sessions(paths, project, provider="codex", session_ids=["session-1"], output=output)
    exported = Envelope.model_validate_json((output / "events.jsonl").read_bytes())
    exported_provider = exported.payload["codex"]
    assert isinstance(exported_provider, dict)
    exported_metadata = exported_provider["metadata"]
    assert isinstance(exported_metadata, dict)
    assert exported_metadata["reasoning_effort"] == "high"
    assert "level=WARNING component=daemon event=spool decision=received reason=unknown_field" in (
        paths.data / "watchdog.log"
    ).read_text(encoding="ascii")
