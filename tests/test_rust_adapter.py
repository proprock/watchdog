import io
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_watchdog.config import (
    Config,
    ConfigError,
    Limits,
    Project,
    UserPaths,
    load_config,
    save_config,
)
from agent_watchdog.daemon import _drain_spool, write_spool_limits
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Inbox, Store, writer_lock


@pytest.fixture
def native_setup(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=root)
    save_config(paths.config, Config(projects=(project,)))
    paths.data.mkdir()
    # Exercise compatibility with the existing daemon ownership lock without launching one.
    with writer_lock(paths.data / "daemon.lock"):
        yield paths, project


def invoke(binary, paths, payload, provider="codex", extra_args=()):
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
            *extra_args,
            "hook",
            provider,
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=2,
    )


def spool_files(paths):
    directory = paths.data / "spool"
    if not directory.is_dir():
        return []
    return [entry for entry in directory.glob("*.json") if entry.name != "limits.json"]


def drain_spool(paths):
    """Run the daemon's spool resolution once, synchronously, as `_poll` would.

    `_poll` skips the drain when the config is unreadable, so mirror that.
    """
    try:
        config = load_config(paths.config)
    except ConfigError:
        return
    _drain_spool(paths, config)


def inbox_files(paths, project):
    return list((paths.project_data(project.id) / "inbox").glob("*.json"))


def test_native_spools_then_daemon_resolves_and_admits(rust_adapter, native_setup):
    paths, project = native_setup
    result = invoke(
        rust_adapter,
        paths,
        {
            "cwd": str(project.root),
            "session_id": "native",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "explain code; password=private-value",
        },
    )
    assert result.returncode == 0 and result.stdout == "{}\n" and not result.stderr
    # The adapter only spools; nothing reaches the project inbox until the daemon drains.
    spooled = spool_files(paths)
    assert len(spooled) == 1
    record = json.loads(spooled[0].read_bytes())
    assert record["cwd"] == str(project.root) and record["provider"] == "codex"
    assert b"private-value" in spooled[0].read_bytes()
    assert not inbox_files(paths, project)

    drain_spool(paths)
    assert not spool_files(paths)
    incoming = inbox_files(paths, project)
    assert len(incoming) == 1 and b"private-value" in incoming[0].read_bytes()
    envelope = Envelope.model_validate_json(incoming[0].read_bytes())
    assert str(envelope.event_id) == record["event_id"]
    assert envelope.session_id == "native" and envelope.kind == "turn.start"
    assert "explain code" in envelope.model_dump_json()
    root = paths.project_data(project.id)
    with Store(root, project.id) as store:
        assert Inbox(root).drain(store).inserted == 1
        assert store.events() == [envelope]


def test_native_v1_limits_default_pipeline_telemetry_and_sample_spool_occupancy(
    rust_adapter, native_setup
):
    paths, project = native_setup
    spool = paths.data / "spool"
    spool.mkdir()
    (spool / "limits.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "payload_bytes": 1024 * 1024,
                "spool_bytes": 64 * 1024 * 1024,
                "spool_files": 4096,
            }
        ),
        encoding="utf-8",
    )

    first_id = "first"
    assert (
        invoke(rust_adapter, paths, {"cwd": str(project.root), "session_id": first_id}).stdout
        == "{}\n"
    )
    first_path = spool_files(paths)[0]
    first = json.loads(first_path.read_bytes())
    assert set(first["delivery"]) == {
        "adapter_started_at",
        "spool_enqueued_at",
        "spool_occupancy",
    }
    adapter_started_at = datetime.fromisoformat(first["delivery"]["adapter_started_at"])
    spool_enqueued_at = datetime.fromisoformat(first["delivery"]["spool_enqueued_at"])
    assert adapter_started_at.tzinfo == UTC
    assert spool_enqueued_at.tzinfo == UTC
    assert adapter_started_at <= spool_enqueued_at
    assert first["delivery"]["spool_occupancy"] == {"files": 0, "bytes": 0}

    assert (
        invoke(rust_adapter, paths, {"cwd": str(project.root), "session_id": "second"}).stdout
        == "{}\n"
    )
    second = next(
        json.loads(path.read_bytes())
        for path in spool_files(paths)
        if json.loads(path.read_bytes())["input"]["session_id"] == "second"
    )
    assert second["delivery"]["spool_occupancy"] == {
        "files": 1,
        "bytes": first_path.stat().st_size,
    }


def test_native_omits_pipeline_telemetry_when_disabled(rust_adapter, native_setup):
    paths, project = native_setup
    config = Config(projects=(project,), pipeline_telemetry=False)
    save_config(paths.config, config)
    write_spool_limits(paths, config)

    assert invoke(rust_adapter, paths, {"cwd": str(project.root)}).stdout == "{}\n"
    record = json.loads(spool_files(paths)[0].read_bytes())
    assert "delivery" not in record


def test_native_pause_and_config_opt_out(rust_adapter, native_setup):
    from agent_watchdog.daemon import set_desired

    paths, project = native_setup
    save_config(paths.config, Config(defaults=Limits(capture_content=False), projects=(project,)))
    payload = {
        "cwd": str(project.root),
        "hook_event_name": "Stop",
        "last_assistant_message": "omit this",
    }
    assert invoke(rust_adapter, paths, payload).returncode == 0
    drain_spool(paths)
    incoming = inbox_files(paths, project)
    assert len(incoming) == 1 and b"omit this" not in incoming[0].read_bytes()

    set_desired(paths, paused=True)
    assert invoke(rust_adapter, paths, payload).stdout == "{}\n"
    # A paused adapter does not even spool.
    assert not spool_files(paths)
    drain_spool(paths)
    assert len(inbox_files(paths, project)) == 1


def test_native_drain_respects_the_python_control_lock(rust_adapter, native_setup):
    paths, project = native_setup
    assert (
        invoke(rust_adapter, paths, {"cwd": str(project.root), "hook_event_name": "Stop"}).stdout
        == "{}\n"
    )
    assert len(spool_files(paths)) == 1
    with writer_lock(paths.data / "control.lock"):
        drain_spool(paths)
    # The drain could not admit while the control lock was held; the record survives.
    assert not inbox_files(paths, project)
    assert len(spool_files(paths)) == 1
    drain_spool(paths)
    assert len(inbox_files(paths, project)) == 1 and not spool_files(paths)


def test_native_expanded_unicode_payload_is_rejected_at_drain(rust_adapter, native_setup):
    from agent_watchdog.resources import losses

    paths, project = native_setup
    save_config(paths.config, Config(defaults=Limits(payload_bytes=4096), projects=(project,)))
    payload = {
        "cwd": str(project.root),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "界" * 600,
    }
    # 1800 raw bytes clear the adapter's fallback cap; ensure_ascii expansion in the
    # daemon's canonical form pushes it past the project's 4096-byte limit.
    assert invoke(rust_adapter, paths, payload).stdout == "{}\n"
    assert len(spool_files(paths)) == 1
    drain_spool(paths)
    assert not inbox_files(paths, project)
    assert losses(paths.project_data(project.id))["payload"] == 1


@pytest.mark.parametrize(
    "native",
    [
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "Interrupt",
        "FutureEvent",
    ],
)
def test_native_matches_python_event_contract(rust_adapter, native_setup, monkeypatch, native):
    from agent_watchdog.hooks import observe

    paths, project = native_setup
    captured = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: captured.append(event)
    )
    payload = {
        "cwd": str(project.root),
        "hook_event_name": native,
        "session_id": "session",
        "turn_id": "turn",
        "agent_id": "agent",
        "tool_name": "exec",
        "tool_use_id": "call",
        "prompt": "password=private-value; " + "sk-proj-" + "a" * 40,
        "tool_input": {"password": "private-value", "command": "echo hello"},
        "tool_response": {"output": "Authorization: Basic private-value", "exit_code": 1},
        "last_assistant_message": "-----BEGIN PRIVATE KEY-----\nprivate-value",
        "transcript_path": str(project.root / ".codex" / "rollout.jsonl"),
        "model": "gpt-test",
        "reasoning_effort": "high",
        "input_tokens": 123,
        "future_metric": {"password": "private-value", "value": 7},
    }
    observe(paths, io.BytesIO(json.dumps(payload).encode()))
    assert invoke(rust_adapter, paths, payload).stdout == "{}\n"
    drain_spool(paths)
    incoming = inbox_files(paths, project)[0]
    actual = Envelope.model_validate_json(incoming.read_bytes())
    exclude = {"received_at", "event_id", "delivery"}
    assert actual.model_dump(exclude=exclude) == captured[0].model_dump(exclude=exclude)
    provider_payload = actual.payload["codex"]
    assert isinstance(provider_payload, dict)
    metadata = provider_payload["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["model"] == "gpt-test" and metadata["input_tokens"] == 123
    assert metadata["future_metric"] == {"password": "private-value", "value": 7}
    assert provider_payload["unknown_fields"] == ["future_metric"]


@pytest.mark.parametrize("lock_name", ["admission.lock", "losses.lock"])
def test_native_drain_respects_project_locks(rust_adapter, native_setup, lock_name):
    from agent_watchdog.resources import losses

    paths, project = native_setup
    root = paths.project_data(project.id)
    root.mkdir(parents=True)
    assert invoke(rust_adapter, paths, {"cwd": str(project.root)}).stdout == "{}\n"
    with writer_lock(root / lock_name):
        drain_spool(paths)
    if lock_name == "admission.lock":
        # Admission cannot proceed; the busy loss is recorded and the record kept.
        assert not inbox_files(paths, project)
        assert len(spool_files(paths)) == 1
        assert losses(root)["busy"] == 1
    else:
        # The losses lock does not gate admission.
        assert len(inbox_files(paths, project)) == 1
        assert losses(root)["busy"] == 0
    drain_spool(paths)
    assert len(inbox_files(paths, project)) == 1 and not spool_files(paths)


def test_native_concurrent_spool_then_quota_enforced_at_drain(rust_adapter, native_setup):
    from concurrent.futures import ThreadPoolExecutor

    from agent_watchdog.resources import losses, usage

    paths, project = native_setup
    limits = Limits(payload_bytes=2048, inbox_bytes=4096)
    save_config(paths.config, Config(defaults=limits, projects=(project,)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: invoke(rust_adapter, paths, {"cwd": str(project.root)}), range(8))
        )
    assert all(result.stdout == "{}\n" for result in results)
    spooled = spool_files(paths)
    assert len(spooled) == 8
    ids = {json.loads(entry.read_bytes())["event_id"] for entry in spooled}
    assert len(ids) == 8
    assert not list((paths.data / "spool").glob("*.tmp"))

    drain_spool(paths)
    root = paths.project_data(project.id)
    incoming = inbox_files(paths, project)
    assert 0 < len(incoming) < 8
    assert usage(root / "inbox") <= limits.inbox_bytes
    assert len(incoming) + losses(root)["quota"] == 8
    assert len({Envelope.model_validate_json(p.read_bytes()).event_id for p in incoming}) == len(
        incoming
    )


def test_native_unregistered_and_invalid_config_fail_open(rust_adapter, native_setup, tmp_path):
    paths, project = native_setup
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    assert invoke(rust_adapter, paths, {"cwd": str(foreign)}).stdout == "{}\n"
    drain_spool(paths)
    assert not inbox_files(paths, project)
    assert not spool_files(paths)  # unregistered cwd is discarded at drain

    paths.config.write_text("schema_version = 999\n", encoding="utf-8")
    assert invoke(rust_adapter, paths, {"cwd": str(project.root)}).stdout == "{}\n"
    drain_spool(paths)
    assert not list(paths.data.glob("projects/*/inbox/*.json"))


def test_native_spool_auto_adds_a_trusted_git_project(rust_adapter, native_setup, tmp_path):
    paths, _ = native_setup
    trusted = tmp_path / "repos"
    repository = trusted / "new-project"
    repository.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    save_config(paths.config, Config(auto_add_projects=True, trusted_projects_dir=trusted))

    assert (
        invoke(rust_adapter, paths, {"cwd": str(repository), "hook_event_name": "Stop"}).stdout
        == "{}\n"
    )
    drain_spool(paths)

    config = load_config(paths.config)
    assert len(config.projects) == 1
    project = config.projects[0]
    assert project.root == repository.resolve()
    assert len(inbox_files(paths, project)) == 1


def test_copied_native_binary_starts_python_core_and_preserves_worktrees(rust_adapter, tmp_path):
    import shutil
    import time

    from agent_watchdog.daemon import status, stop
    from agent_watchdog.registry import Registry

    binary = tmp_path / ("installed adapter" + rust_adapter.suffix)
    shutil.copy2(rust_adapter, binary)
    root = tmp_path / "repository with spaces"
    worktree = tmp_path / "worktree with spaces"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=15)

    git("init")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    git("worktree", "add", "--detach", str(worktree))
    registry = Registry()
    project = registry.add(root)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, registry.config)

    def wait(predicate):
        # A generous margin: a cross-compiled binary under Rosetta 2 pays a
        # one-time JIT-translation cost on first launch that a native build
        # does not, on top of ordinary CI runner variance.
        deadline = time.monotonic() + 30
        while not predicate():
            assert time.monotonic() < deadline, "Native-launched core did not settle"
            time.sleep(0.05)

    try:
        for checkout in (root, worktree):
            assert (
                invoke(binary, paths, {"cwd": str(checkout), "session_id": checkout.name}).stdout
                == "{}\n"
            )
        wait(lambda: status(paths)["state"] == "running")
        wait(lambda: not spool_files(paths))
        wait(lambda: not list(paths.project_data(project.id).glob("inbox/*.json")))
    finally:
        stop(paths)
        wait(lambda: not status(paths)["alive"])
    with Store(paths.project_data(project.id), project.id) as store:
        events = store.events()
        assert len(events) == 2
        main = registry.resolve(root)
        linked = registry.resolve(worktree)
        assert main is not None and linked is not None
        assert {event.checkout_id for event in events} == {
            main.checkout_id,
            linked.checkout_id,
        }


CLAUDE_EVENTS = [
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Notification",
]


@pytest.mark.parametrize("native", CLAUDE_EVENTS)
def test_native_matches_python_claude_contract(rust_adapter, native_setup, monkeypatch, native):
    from agent_watchdog.hooks import observe

    paths, project = native_setup
    captured = []
    monkeypatch.setattr(
        "agent_watchdog.hooks.daemon.enqueue", lambda paths, event: captured.append(event)
    )
    payload = {
        "cwd": str(project.root),
        "hook_event_name": native,
        "session_id": "session",
        "agent_id": "agent",
        "tool_name": "exec",
        "tool_use_id": "call",
        "prompt": "password=private-value; sk-proj-" + "a" * 40,
        "tool_input": {"password": "private-value", "command": "echo hello"},
        "tool_response": {"output": "Authorization: Basic private-value", "exit_code": 1},
        "last_assistant_message": "-----BEGIN PRIVATE KEY-----\nprivate-value",
    }
    observe(paths, io.BytesIO(json.dumps(payload).encode()), "claude")
    assert invoke(rust_adapter, paths, payload, provider="claude").stdout == ""
    drain_spool(paths)
    incoming = inbox_files(paths, project)[0]
    actual = Envelope.model_validate_json(incoming.read_bytes())
    exclude = {"received_at", "event_id", "delivery"}
    assert actual.model_dump(exclude=exclude) == captured[0].model_dump(exclude=exclude)
    assert actual.provider == "claude" and "claude" in actual.payload


def test_native_claude_emits_empty_stdout_and_codex_emits_object(rust_adapter, native_setup):
    paths, project = native_setup
    payload = {"cwd": str(project.root), "hook_event_name": "Stop"}
    assert invoke(rust_adapter, paths, payload, provider="claude").stdout == ""
    assert invoke(rust_adapter, paths, payload, provider="codex").stdout == "{}\n"


def test_native_invalid_args_with_claude_token_stay_silent(rust_adapter, native_setup):
    paths, _ = native_setup
    result = subprocess.run(
        [str(rust_adapter), "--config", str(paths.config), "bogus", "hook", "claude"],
        input="{}",
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode == 0 and result.stdout == "" and not result.stderr


def test_native_unknown_provider_token_is_rejected(rust_adapter, native_setup):
    paths, project = native_setup
    result = invoke(
        rust_adapter,
        paths,
        {"cwd": str(project.root), "hook_event_name": "Stop"},
        provider="banana",
    )
    assert result.returncode == 0 and result.stdout == "{}\n"
    assert not spool_files(paths)


@pytest.fixture
def native_git_setup(tmp_path):
    from agent_watchdog.registry import Registry

    root = tmp_path / "repo with spaces"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=15)

    git("init")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    worktree = tmp_path / "worktree with spaces"
    git("worktree", "add", "--detach", str(worktree))
    (root / "sub").mkdir()
    (worktree / "nested").mkdir()

    registry = Registry()
    project = registry.add(root)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, registry.config)
    paths.data.mkdir()
    with writer_lock(paths.data / "daemon.lock"):
        yield paths, project, registry, root, worktree


def test_native_spooled_cwd_resolves_like_the_registry(rust_adapter, native_git_setup):
    """The adapter spools `cwd` verbatim; the daemon's drain resolves it, and the
    result must match the Python registry that test_checkout_resolution.py pins to
    `git rev-parse`."""
    paths, project, registry, root, worktree = native_git_setup
    inbox = paths.project_data(project.id) / "inbox"
    seen = {}
    for cwd in (root, root / "sub", worktree, worktree / "nested"):
        for leftover in inbox.glob("*.json"):
            leftover.unlink()
        result = invoke(rust_adapter, paths, {"cwd": str(cwd), "hook_event_name": "Stop"})
        assert result.stdout == "{}\n"
        drain_spool(paths)
        incoming = list(inbox.glob("*.json"))
        assert len(incoming) == 1
        envelope = Envelope.model_validate_json(incoming[0].read_bytes())
        expected = registry.resolve(cwd)
        assert expected is not None
        assert envelope.project_id == project.id
        assert str(envelope.checkout_id) == str(expected.checkout_id)
        seen[cwd] = envelope.checkout_id
    assert seen[root] == seen[root / "sub"]
    assert seen[worktree] == seen[worktree / "nested"]
    assert seen[root] != seen[worktree]


def test_native_records_attributable_fault_reason(rust_adapter, native_setup):
    from agent_watchdog.resources import losses

    paths, _ = native_setup
    result = invoke(rust_adapter, paths, {"cwd": "relative/path", "hook_event_name": "Stop"})
    assert result.returncode == 0 and result.stdout == "{}\n"
    assert losses(paths.data)["invalid"] == 1
    log = (paths.data / "faults.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 1
    stamp, event, reason = log[0].split(" ")
    assert event == "Stop" and reason == "cwd-relative"
    assert stamp.endswith("Z")


def test_native_claude_shares_pause_and_payload_limit_paths(rust_adapter, native_setup):
    from agent_watchdog.daemon import set_desired
    from agent_watchdog.resources import losses

    paths, project = native_setup
    save_config(paths.config, Config(defaults=Limits(payload_bytes=4096), projects=(project,)))
    # Publish the snapshot a running daemon would, so the adapter enforces the cap itself.
    write_spool_limits(paths, load_config(paths.config))
    big = {"cwd": str(project.root), "hook_event_name": "UserPromptSubmit", "prompt": "x" * 8000}
    assert invoke(rust_adapter, paths, big, provider="claude").stdout == ""
    assert not spool_files(paths) and losses(paths.data)["payload"] == 1

    set_desired(paths, paused=True)
    stop_payload = {"cwd": str(project.root), "hook_event_name": "Stop"}
    assert invoke(rust_adapter, paths, stop_payload, provider="claude").stdout == ""
    assert not spool_files(paths)
    drain_spool(paths)
    assert not inbox_files(paths, project)


AGENT_SUBSCRIPTION = {"provider": "claude", "hook_event_name": "PreToolUse", "tool_name": "Agent"}
ALL_CLAUDE_EVENTS = [
    {"provider": "claude", "hook_event_name": name}
    for name in (
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionStart",
        "PreCompact",
        "Stop",
        "SubagentStop",
    )
]


class _FakePolicyServer:
    """Stands in for the daemon's decision channel in adapter-only tests.

    Serves every connection until closed, so a test can assert both what was
    answered and that an unsubscribed hook never connected at all.
    """

    def __init__(self, paths, *, token="0" * 32, discovery=None, subscriptions=None):
        self.token = token
        self.connections = 0
        self.requests = []
        self._reply = b""
        self._hang = False
        self._closed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.1)
        self.port = self._socket.getsockname()[1]
        policy = paths.data / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 2,
            "pid": 0,
            "port": self.port,
            "token": self.token,
            "timeout_ms": 300,
            "max_request_bytes": 1024**2 + 64 * 1024,
            "subscriptions": [AGENT_SUBSCRIPTION] if subscriptions is None else subscriptions,
        }
        document.update(discovery or {})
        (policy / "socket.json").write_text(json.dumps(document), encoding="utf-8")
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with connection:
                connection.settimeout(1)
                data = b""
                try:
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    continue
                try:
                    self.requests.append(json.loads(data))
                except ValueError:
                    self.requests.append(None)
                if self._hang:
                    # Hold the connection open until the test ends: closing it
                    # early would hand the adapter an EOF, which also fails
                    # open and would hide a missing read deadline.
                    self._closed.wait()
                    continue
                try:
                    connection.sendall(self._reply)
                except OSError:
                    continue

    def respond(self, action="allow", **fields):
        """Answer every request with one decision (schema 2 by default)."""
        body = {"schema_version": 2, "action": action, **fields}
        self._reply = json.dumps(body).encode("utf-8")
        return self

    def respond_raw(self, payload):
        self._reply = payload
        return self

    def respond_malformed(self):
        return self.respond_raw(b"not json")

    def hang(self):
        self._hang = True
        return self

    def connected(self):
        """Connection count after the accept loop has had time to see any."""
        time.sleep(0.3)
        return self.connections

    def close(self):
        self._closed.set()
        self._socket.close()


def agent_pretooluse(project, *, model):
    return {
        "cwd": str(project.root),
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose", "prompt": "p", "model": model},
    }


def test_native_denies_a_same_model_subagent_spawn(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("deny")
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
    finally:
        server.close()
    assert result.returncode == 0
    stdout = json.loads(result.stdout)
    assert stdout["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert stdout["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert isinstance(stdout["hookSpecificOutput"]["permissionDecisionReason"], str)
    # The log half of `both` always fires, even when denied.
    assert spool_files(paths)


def test_native_allows_a_different_model_subagent_spawn(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("allow")
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="haiku"), provider="claude"
        )
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_fails_open_without_a_policy_socket_file(rust_adapter, native_setup):
    paths, project = native_setup
    result = invoke(rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude")
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_fails_open_on_a_stale_socket_file(rust_adapter, native_setup):
    """A closed port behind a leftover socket.json must never produce a deny."""
    paths, project = native_setup
    dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    policy = paths.data / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "socket.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "pid": 0,
                "port": port,
                "token": "x" * 32,
                "timeout_ms": 300,
                "max_request_bytes": 1024**2,
                "subscriptions": [AGENT_SUBSCRIPTION],
            }
        ),
        encoding="utf-8",
    )
    result = invoke(rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude")
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_fails_open_on_a_malformed_response(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond_malformed()
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


def hook_input(project, event, **extra):
    return {"cwd": str(project.root), "session_id": "session-1", "hook_event_name": event, **extra}


def test_native_sends_the_full_hook_input_and_the_spooled_event_id(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("allow")
    try:
        payload = agent_pretooluse(project, model="opus")
        invoke(rust_adapter, paths, payload, provider="claude")
        assert server.connected() == 1
    finally:
        server.close()
    request = server.requests[0]
    assert request["schema_version"] == 2
    assert request["token"] == server.token
    assert request["provider"] == "claude"
    assert request["input"] == payload
    (spooled,) = spool_files(paths)
    assert request["event_id"] == json.loads(spooled.read_text(encoding="utf-8"))["event_id"]


RENDERED_CASES = [
    pytest.param(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"action": "deny", "reason": "no"},
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "no",
            }
        },
        id="pretooluse-deny",
    ),
    pytest.param(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"action": "ask", "reason": "sure?"},
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "sure?",
            }
        },
        id="pretooluse-ask",
    ),
    pytest.param(
        "PreToolUse",
        {"tool_name": "Agent", "tool_input": {"prompt": "p", "model": "opus"}},
        {
            "action": "rewrite",
            "reason": "downgraded",
            "updated_input": {"prompt": "p", "model": "sonnet"},
        },
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "downgraded",
                "updatedInput": {"prompt": "p", "model": "sonnet"},
            }
        },
        id="pretooluse-rewrite",
    ),
    *[
        pytest.param(
            event,
            {},
            {"action": "context", "context": "TOKEN"},
            {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "TOKEN"}},
            id=f"{event.lower()}-context",
        )
        for event in ("PostToolUse", "UserPromptSubmit", "SessionStart", "PreCompact")
    ],
    *[
        pytest.param(
            event,
            {},
            {"action": "block", "reason": "again"},
            {"decision": "block", "reason": "again"},
            id=f"{event.lower()}-block",
        )
        for event in ("PostToolUse", "Stop", "SubagentStop")
    ],
]


@pytest.mark.parametrize(("event", "extra", "answer", "expected"), RENDERED_CASES)
def test_native_renders_each_confirmed_claude_action(
    rust_adapter, native_setup, event, extra, answer, expected
):
    paths, project = native_setup
    server = _FakePolicyServer(paths, subscriptions=ALL_CLAUDE_EVENTS).respond(**answer)
    try:
        result = invoke(rust_adapter, paths, hook_input(project, event, **extra), provider="claude")
    finally:
        server.close()
    assert result.returncode == 0
    assert json.loads(result.stdout) == expected
    assert spool_files(paths)


@pytest.mark.parametrize(
    ("event", "answer"),
    [
        pytest.param("PreToolUse", {"action": "block", "reason": "x"}, id="pretooluse-block"),
        # Only weakly evidenced live (WD-139: absence of an effect), so not rendered.
        pytest.param("PreCompact", {"action": "block", "reason": "x"}, id="precompact-block"),
        pytest.param("Stop", {"action": "context", "context": "x"}, id="stop-context"),
        pytest.param("Stop", {"action": "deny", "reason": "x"}, id="stop-deny"),
        pytest.param("UserPromptSubmit", {"action": "block", "reason": "x"}, id="prompt-block"),
        pytest.param("SessionStart", {"action": "deny", "reason": "x"}, id="start-deny"),
        pytest.param("PreToolUse", {"action": "context"}, id="context-without-text"),
        pytest.param("PreToolUse", {"action": "rewrite"}, id="rewrite-without-input"),
        pytest.param("PreToolUse", {"action": "allow"}, id="allow-is-silence"),
    ],
)
def test_native_renders_nothing_for_an_unsupported_combination(
    rust_adapter, native_setup, event, answer
):
    paths, project = native_setup
    server = _FakePolicyServer(paths, subscriptions=ALL_CLAUDE_EVENTS).respond(**answer)
    try:
        result = invoke(rust_adapter, paths, hook_input(project, event), provider="claude")
        assert server.connected() == 1
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


@pytest.mark.parametrize("event", ["Stop", "SubagentStop"])
@pytest.mark.parametrize("active", [True, "yes", 1])
def test_native_never_asks_about_a_stop_that_is_already_continuing(
    rust_adapter, native_setup, event, active
):
    paths, project = native_setup
    server = _FakePolicyServer(paths, subscriptions=ALL_CLAUDE_EVENTS).respond(
        "block", reason="again"
    )
    try:
        result = invoke(
            rust_adapter,
            paths,
            hook_input(project, event, stop_hook_active=active),
            provider="claude",
        )
        assert server.connected() == 0
    finally:
        server.close()
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_asks_about_a_stop_that_is_not_continuing(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths, subscriptions=ALL_CLAUDE_EVENTS).respond(
        "block", reason="again"
    )
    try:
        result = invoke(
            rust_adapter,
            paths,
            hook_input(project, "Stop", stop_hook_active=False),
            provider="claude",
        )
    finally:
        server.close()
    assert json.loads(result.stdout) == {"decision": "block", "reason": "again"}


def test_native_never_connects_for_an_unsubscribed_event(rust_adapter, native_setup):
    """A server that would always deny must never be reached for a hook outside
    `subscriptions`: another tool, another event, another provider."""
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("deny", reason="no")
    try:
        bash = hook_input(project, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
        results = [
            invoke(rust_adapter, paths, bash, provider="claude"),
            invoke(rust_adapter, paths, hook_input(project, "Stop"), provider="claude"),
            invoke(rust_adapter, paths, agent_pretooluse(project, model="opus")),
        ]
        connections = server.connected()
    finally:
        server.close()
    assert connections == 0
    assert [result.stdout for result in results] == ["", "", "{}\n"]
    assert len(spool_files(paths)) == 3


def test_native_asks_about_an_agent_spawn_without_a_model_override(rust_adapter, native_setup):
    """The adapter no longer pre-filters on `tool_input.model`; the daemon's rule
    decides, so a model-less spawn costs one loopback round trip and nothing
    else."""
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("allow")
    try:
        payload = agent_pretooluse(project, model="opus")
        del payload["tool_input"]["model"]
        result = invoke(rust_adapter, paths, payload, provider="claude")
        assert server.connected() == 1
    finally:
        server.close()
    assert result.stdout == ""


RENDER_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "SessionStart",
    "PreCompact",
    "Stop",
    "SubagentStop",
)
RENDER_ACTIONS = ("deny", "ask", "rewrite", "context", "block")


@pytest.mark.parametrize("action", RENDER_ACTIONS)
@pytest.mark.parametrize("event", RENDER_EVENTS)
def test_native_renders_exactly_the_cells_the_daemon_may_return(
    rust_adapter, native_setup, event, action
):
    """The adapter's `render` and the daemon's `rules.api.renderable` are two
    tables for one fact; this keeps them equal, cell by cell."""
    from agent_watchdog.rules.api import renderable

    paths, project = native_setup
    server = _FakePolicyServer(paths, subscriptions=ALL_CLAUDE_EVENTS).respond(
        action, reason="r", context="c", updated_input={"command": "ls"}
    )
    try:
        result = invoke(rust_adapter, paths, hook_input(project, event), provider="claude")
    finally:
        server.close()
    assert (result.stdout != "") == renderable("claude", event, action)


def test_native_never_renders_a_decision_for_codex(rust_adapter, native_setup):
    paths, project = native_setup
    subscriptions = [{"provider": "codex", "hook_event_name": "PreToolUse"}]
    server = _FakePolicyServer(paths, subscriptions=subscriptions).respond("deny", reason="no")
    try:
        payload = hook_input(project, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
        result = invoke(rust_adapter, paths, payload, provider="codex")
    finally:
        server.close()
    assert result.stdout == "{}\n"
    assert spool_files(paths)


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(lambda server: server.respond_malformed(), id="malformed-response"),
        pytest.param(lambda server: server.respond_raw(b""), id="empty-response"),
        pytest.param(
            lambda server: server.respond_raw(
                json.dumps({"schema_version": 1, "decision": "deny"}).encode()
            ),
            id="schema-1-response",
        ),
        pytest.param(lambda server: server.respond("explode"), id="unknown-action"),
        pytest.param(
            lambda server: server.respond_raw(
                json.dumps(
                    {"schema_version": 2, "action": "deny", "reason": "x" * 2_000_000}
                ).encode()
            ),
            id="oversized-response",
        ),
        pytest.param(lambda server: server.respond("deny", reason="no").hang(), id="timeout"),
    ],
)
def test_native_fails_open_on_a_bad_decision(rust_adapter, native_setup, configure):
    paths, project = native_setup
    server = configure(_FakePolicyServer(paths))
    started = time.monotonic()
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)
    assert time.monotonic() - started < 1.9


def test_native_gives_up_on_a_silent_daemon_after_the_published_budget(rust_adapter, native_setup):
    """The 300 ms budget, not the 1500 ms clamp or the two-second hook timeout,
    bounds a daemon that accepts the connection and never answers."""
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("deny", reason="no").hang()
    started = time.monotonic()
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
    finally:
        server.close()
    assert result.stdout == ""
    assert spool_files(paths)
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "discovery",
    [
        pytest.param({"schema_version": 1}, id="schema-1"),
        pytest.param({"schema_version": 3}, id="future-schema"),
        pytest.param({"token": ""}, id="empty-token"),
        pytest.param({"port": 70000}, id="port-out-of-range"),
        pytest.param({"max_request_bytes": 0}, id="no-request-budget"),
        pytest.param({"subscriptions": "everything"}, id="subscriptions-not-a-list"),
        pytest.param({"subscriptions": [{"provider": "claude"}]}, id="subscription-incomplete"),
        pytest.param({"max_request_bytes": 10}, id="request-over-budget"),
    ],
)
def test_native_fails_open_on_an_unusable_discovery_file(rust_adapter, native_setup, discovery):
    paths, project = native_setup
    server = _FakePolicyServer(paths, discovery=discovery).respond("deny", reason="no")
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
        assert server.connected() == 0
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_fails_open_on_an_oversized_discovery_file(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths, discovery={"padding": "x" * 20_000}).respond(
        "deny", reason="no"
    )
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
        assert server.connected() == 0
    finally:
        server.close()
    assert result.stdout == ""
    assert spool_files(paths)


def test_native_clamps_a_huge_published_timeout(rust_adapter, native_setup):
    """The published timeout may never push a hang past the two-second hook timeout."""
    paths, project = native_setup
    server = _FakePolicyServer(paths, discovery={"timeout_ms": 600_000}).respond("deny").hang()
    started = time.monotonic()
    try:
        result = invoke(
            rust_adapter, paths, agent_pretooluse(project, model="opus"), provider="claude"
        )
    finally:
        server.close()
    assert result.stdout == ""
    assert time.monotonic() - started < 1.9


def test_native_sends_a_large_request_within_the_published_budget(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("deny", reason="no")
    try:
        payload = agent_pretooluse(project, model="opus")
        payload["tool_input"]["prompt"] = "x" * 500_000
        result = invoke(rust_adapter, paths, payload, provider="claude")
    finally:
        server.close()
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(server.requests[0]["input"]["tool_input"]["prompt"]) == 500_000


def test_native_deny_round_trips_through_a_real_running_daemon(rust_adapter, tmp_path):
    """End-to-end: a real daemon (launched by the adapter itself, exactly as in
    production) answers a real PreToolUse deny query over the loopback policy
    socket, not a fake stand-in server."""
    import time

    from agent_watchdog.daemon import status, stop
    from agent_watchdog.registry import Registry

    root = tmp_path / "project"
    root.mkdir()
    registry = Registry()
    project = registry.add(root)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, registry.config)

    def wait(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, "Daemon did not settle"
            time.sleep(0.05)

    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id="session-1",
                kind="usage",
                source="transcript",
                received_at=datetime.now(UTC),
                payload={"claude": {"model": "claude-opus-5-5"}},
            )
        )
    try:
        # Get the daemon running first, exactly as a real hook sequence would.
        invoke(rust_adapter, paths, {"cwd": str(root), "hook_event_name": "SessionStart"})
        wait(lambda: status(paths)["state"] == "running")
        wait(lambda: (paths.data / "policy" / "socket.json").is_file())

        # Deny first: if the daemon's very first policy response were ever slow
        # enough to overrun the adapter's own read timeout, an "allow" sent
        # first would fail open and pass this assertion for the wrong reason.
        # A denied case sent first fails loudly instead of silently passing.
        denied = invoke(
            rust_adapter,
            paths,
            {
                "cwd": str(root),
                "session_id": "session-1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "general-purpose", "prompt": "p", "model": "opus"},
            },
            provider="claude",
        )
        stdout = json.loads(denied.stdout)
        assert stdout["hookSpecificOutput"]["permissionDecision"] == "deny"

        allowed = invoke(
            rust_adapter,
            paths,
            {
                "cwd": str(root),
                "session_id": "session-1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "general-purpose", "prompt": "p", "model": "haiku"},
            },
            provider="claude",
        )
        assert allowed.stdout == ""
    finally:
        stop(paths)
        wait(lambda: not status(paths)["alive"])
    assert not (paths.data / "policy" / "socket.json").exists()


def test_native_denies_a_very_long_prompt_and_records_a_control_event_with_a_real_daemon(
    rust_adapter, tmp_path
):
    """A long subagent prompt must still reach the daemon (the request budget
    follows the adapter's own stdin cap), and the delivered deny is recorded as
    a `control` event."""
    from agent_watchdog.daemon import status, stop
    from agent_watchdog.inspection import database
    from agent_watchdog.registry import Registry

    root = tmp_path / "project"
    root.mkdir()
    registry = Registry()
    project = registry.add(root)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, registry.config)

    def wait(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, "Daemon did not settle"
            time.sleep(0.05)

    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id="session-1",
                kind="usage",
                source="transcript",
                received_at=datetime.now(UTC),
                payload={"claude": {"model": "claude-opus-5-5"}},
            )
        )

    def control_rows():
        # Read-only: the running daemon holds the writer lock.
        with database(paths, project) as db:
            return db.execute("SELECT event_id FROM event_facts WHERE kind='control'").fetchall()

    try:
        invoke(rust_adapter, paths, {"cwd": str(root), "hook_event_name": "SessionStart"})
        wait(lambda: status(paths)["state"] == "running")
        wait(lambda: (paths.data / "policy" / "socket.json").is_file())
        payload = {
            "cwd": str(root),
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": "x" * 900_000, "model": "opus"},
        }
        denied = invoke(rust_adapter, paths, payload, provider="claude")
        assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        wait(lambda: control_rows())
    finally:
        stop(paths)
        wait(lambda: not status(paths)["alive"])


def test_native_never_queries_a_non_agent_tool(rust_adapter, native_setup):
    paths, project = native_setup
    server = _FakePolicyServer(paths).respond("deny")
    try:
        payload = {
            "cwd": str(project.root),
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi", "model": "opus"},
        }
        result = invoke(rust_adapter, paths, payload, provider="claude")
    finally:
        server.close()
    assert result.returncode == 0
    assert result.stdout == ""
    assert spool_files(paths)
