import io
import json
import os
import subprocess
import sys
from pathlib import Path
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
def rust_adapter():
    suffix = ".exe" if os.name == "nt" else ""
    binary = (
        Path(__file__).parents[1] / "native" / "target" / "release" / f"agent-watchdog-hook{suffix}"
    )
    assert binary.is_file(), (
        "Build the Rust adapter with cargo build --release --manifest-path native/Cargo.toml"
    )
    return binary


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
    assert b"private-value" not in spooled[0].read_bytes()
    assert not inbox_files(paths, project)

    drain_spool(paths)
    assert not spool_files(paths)
    incoming = inbox_files(paths, project)
    assert len(incoming) == 1 and b"private-value" not in incoming[0].read_bytes()
    envelope = Envelope.model_validate_json(incoming[0].read_bytes())
    assert str(envelope.event_id) == record["event_id"]
    assert envelope.session_id == "native" and envelope.kind == "turn.start"
    assert "explain code" in envelope.model_dump_json()
    root = paths.project_data(project.id)
    with Store(root, project.id) as store:
        assert Inbox(root).drain(store).inserted == 1
        assert store.events() == [envelope]


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
    }
    observe(paths, io.BytesIO(json.dumps(payload).encode()))
    assert invoke(rust_adapter, paths, payload).stdout == "{}\n"
    drain_spool(paths)
    incoming = inbox_files(paths, project)[0]
    actual = Envelope.model_validate_json(incoming.read_bytes())
    exclude = {"received_at", "event_id"}
    assert actual.model_dump(exclude=exclude) == captured[0].model_dump(exclude=exclude)


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
        deadline = time.monotonic() + 15
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
    exclude = {"received_at", "event_id"}
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
