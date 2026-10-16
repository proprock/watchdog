import json
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.storage import writer_lock

pytestmark = pytest.mark.native

SUBSCRIPTION = {"provider": "claude", "hook_event_name": "PreToolUse", "tool_name": "Agent"}


@pytest.fixture
def native_control_setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=root)
    save_config(paths.config, Config(projects=(project,)))
    paths.data.mkdir()
    return paths, project


def _spool(paths):
    directory = paths.data / "spool"
    return [p for p in directory.glob("*.json") if p.name != "limits.json"]


class FakePolicy:
    def __init__(self, paths, response=None, *, delay=0, subscriptions=None):
        self.response = response
        self.delay = delay
        self.requests = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        policy = paths.data / "policy"
        policy.mkdir()
        (policy / "socket.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "pid": 1,
                    "port": self.port,
                    "token": "t" * 32,
                    "timeout_ms": 100,
                    "max_request_bytes": 65536,
                    "subscriptions": subscriptions if subscriptions is not None else [SUBSCRIPTION],
                }
            ),
            encoding="utf-8",
        )

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            raw = b""
            while not raw.endswith(b"\n"):
                part = conn.recv(4096)
                if not part:
                    break
                raw += part
            if not raw:
                return
            self.requests.append(json.loads(raw))
            if self.delay:
                time.sleep(self.delay)
            if self.response is not None:
                try:
                    conn.sendall(self.response)
                except OSError:
                    pass  # The timed-out adapter has already failed open.

    def close(self):
        self.listener.close()
        if self.thread.is_alive():
            self.thread.join(timeout=1)


def _invoke(binary, paths, project):
    with writer_lock(paths.data / "daemon.lock"):
        return subprocess.run(
            [
                str(binary),
                "--python",
                sys.executable,
                "--config",
                str(paths.config),
                "--data",
                str(paths.data),
                "--runtime",
                str(paths.runtime),
                "hook",
                "claude",
            ],
            input=json.dumps(
                {
                    "cwd": str(project.root),
                    "session_id": "test",
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Agent",
                    "tool_input": {"command": "echo test"},
                }
            ),
            capture_output=True,
            text=True,
            timeout=3,
        )


def _set_pipeline_telemetry(paths, enabled):
    spool = paths.data / "spool"
    spool.mkdir(exist_ok=True)
    (spool / "limits.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "payload_bytes": 1024 * 1024,
                "spool_bytes": 64 * 1024 * 1024,
                "spool_files": 4096,
                "pipeline_telemetry": enabled,
            }
        ),
        encoding="utf-8",
    )


def _control(paths):
    (record,) = _spool(paths)
    return json.loads(record.read_text(encoding="utf-8")).get("delivery", {}).get("control")


def test_native_control_delivery_records_render_ack_even_without_pipeline_telemetry(
    rust_adapter, native_control_setup
):
    paths, project = native_control_setup
    _set_pipeline_telemetry(paths, False)
    policy = FakePolicy(
        paths,
        json.dumps(
            {
                "schema_version": 2,
                "action": "deny",
                "reason": "blocked",
            }
        ).encode(),
    ).start()
    try:
        result = _invoke(rust_adapter, paths, project)
    finally:
        policy.close()
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _control(paths) == {
        "response_received": True,
        "adapter_rendered": True,
        "requested_action": "deny",
    }


def test_native_control_delivery_preserves_pipeline_telemetry_and_allow_is_silent(
    rust_adapter, native_control_setup
):
    paths, project = native_control_setup
    _set_pipeline_telemetry(paths, True)
    policy = FakePolicy(
        paths,
        json.dumps(
            {
                "schema_version": 2,
                "action": "allow",
            }
        ).encode(),
    ).start()
    try:
        result = _invoke(rust_adapter, paths, project)
    finally:
        policy.close()
    assert result.returncode == 0 and result.stdout == ""
    (record_path,) = _spool(paths)
    delivery = json.loads(record_path.read_text(encoding="utf-8"))["delivery"]
    assert delivery["control"] == {
        "response_received": True,
        "adapter_rendered": None,
        "requested_action": "allow",
    }
    assert "adapter_started_at" in delivery
    assert "spool_enqueued_at" in delivery
    assert delivery["spool_occupancy"] == {"files": 0, "bytes": 0}


@pytest.mark.parametrize(
    "response",
    [
        {"schema_version": 2, "action": "block", "reason": "unsupported here"},
        {"schema_version": 2, "action": "rewrite", "reason": "needs replacement"},
    ],
)
def test_native_control_delivery_records_unrenderable_response(
    rust_adapter, native_control_setup, response
):
    paths, project = native_control_setup
    policy = FakePolicy(paths, json.dumps(response).encode()).start()
    try:
        result = _invoke(rust_adapter, paths, project)
    finally:
        policy.close()
    assert result.returncode == 0 and result.stdout == ""
    assert _control(paths) == {
        "response_received": True,
        "adapter_rendered": False,
        "requested_action": response["action"],
    }


@pytest.mark.parametrize(
    "kind", ["timeout", "disconnected", "wrong-version", "malformed", "missing-action"]
)
def test_native_control_delivery_records_missing_or_invalid_ack(
    rust_adapter, native_control_setup, kind
):
    paths, project = native_control_setup
    if kind == "timeout":
        policy = FakePolicy(
            paths,
            json.dumps(
                {
                    "schema_version": 2,
                    "action": "deny",
                    "reason": "late",
                }
            ).encode(),
            delay=0.3,
        ).start()
    else:
        responses = {
            "disconnected": None,
            "wrong-version": b'{"schema_version":1,"action":"deny"}',
            "malformed": b"{",
            "missing-action": b'{"schema_version":2}',
        }
        policy = FakePolicy(paths, responses[kind]).start()
    try:
        result = _invoke(rust_adapter, paths, project)
    finally:
        policy.close()
    assert result.returncode == 0 and result.stdout == ""
    assert _control(paths) == {
        "response_received": False,
        "adapter_rendered": None,
        "requested_action": None,
    }


def test_native_control_delivery_is_omitted_when_event_is_unsubscribed(
    rust_adapter, native_control_setup
):
    paths, project = native_control_setup
    policy = FakePolicy(
        paths,
        json.dumps(
            {
                "schema_version": 2,
                "action": "deny",
            }
        ).encode(),
        subscriptions=[],
    ).start()
    try:
        result = _invoke(rust_adapter, paths, project)
    finally:
        policy.close()
    assert result.returncode == 0 and result.stdout == ""
    assert _control(paths) is None
    assert not policy.requests
