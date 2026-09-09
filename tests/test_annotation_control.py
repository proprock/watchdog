"""Control-request path for WD-012 annotations: the CLI never writes SQLite."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_watchdog.config import UserPaths, load_config
from agent_watchdog.daemon import _drain_controls, mutate_registry
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store

FINGERPRINT = "a" * 64


@pytest.fixture
def project(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    root = tmp_path / "checkout"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    entry = config.projects[0]
    with Store(paths.project_data(entry.id), entry.id) as store:
        store.put(
            Envelope(
                event_id=uuid4(),
                provider="codex",
                project_id=entry.id,
                checkout_id=entry.id,
                session_id="s1",
                kind="turn.start",
                received_at=datetime.now(UTC),
                source="hook",
                payload={"codex": {"content": "omitted"}},
            )
        )
    return paths, config, entry


def submit(paths, request):
    request_id = str(uuid4())
    directory = paths.data / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{request_id}.json").write_text(
        json.dumps({"schema_version": 1, "request_id": request_id, **request}), encoding="utf-8"
    )
    return request_id


def acknowledgement(paths, request_id):
    return json.loads((paths.data / "acks" / f"{request_id}.json").read_text(encoding="utf-8"))


def test_a_label_request_carries_the_progress_state(project):
    paths, config, entry = project
    request_id = submit(
        paths,
        {
            "action": "label",
            "project_id": str(entry.id),
            "provider": "codex",
            "session_id": "s1",
            "outcome": "partial",
            "task_type": "debug",
            "progress_state": "stuck",
            "reviewer_note": "waited on a flaky external service",
        },
    )

    assert _drain_controls(paths, config) is True
    ack = acknowledgement(paths, request_id)
    assert ack["ok"] is True
    assert ack["result"]["label"]["progress_state"] == "stuck"
    assert ack["result"]["label"]["reviewer_note"] == "waited on a flaky external service"


def test_a_session_verdict_request_is_acknowledged(project):
    paths, config, entry = project
    request_id = submit(
        paths,
        {
            "action": "verdict",
            "project_id": str(entry.id),
            "provider": "codex",
            "session_id": "s1",
            "rule": "repeated_tool_outcome",
            "rule_version": "wd-010.v1",
            "fingerprint": FINGERPRINT,
            "verdict": "true_positive",
            "note": None,
        },
    )

    assert _drain_controls(paths, config) is True
    assert acknowledgement(paths, request_id)["result"]["verdict"]["verdict"] == "true_positive"


def test_a_checkout_verdict_request_needs_no_session(project):
    paths, config, entry = project
    request_id = submit(
        paths,
        {
            "action": "checkout_verdict",
            "project_id": str(entry.id),
            "checkout_id": "9f4edabb-c34e-5c24-a0da-24755024f12d",
            "rule": "diff_oscillation",
            "rule_version": "wd-010.v1",
            "fingerprint": FINGERPRINT,
            "verdict": "uncertain",
            "note": "a concurrent editor cannot be excluded",
        },
    )

    assert _drain_controls(paths, config) is True
    assert acknowledgement(paths, request_id)["result"]["verdict"]["verdict"] == "uncertain"


def test_an_unsupported_verdict_is_rejected_without_writing(project):
    paths, config, entry = project
    request_id = submit(
        paths,
        {
            "action": "verdict",
            "project_id": str(entry.id),
            "provider": "codex",
            "session_id": "s1",
            "rule": "repeated_tool_outcome",
            "rule_version": "wd-010.v1",
            "fingerprint": FINGERPRINT,
            "verdict": "probably",
            "note": None,
        },
    )

    _drain_controls(paths, config)

    ack = acknowledgement(paths, request_id)
    assert ack["ok"] is False and "verdict" in ack["message"]
    with Store(paths.project_data(entry.id), entry.id) as store:
        assert store.connection.execute("SELECT COUNT(*) FROM finding_verdicts").fetchone() == (0,)
