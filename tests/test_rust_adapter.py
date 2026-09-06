import io
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.config import Config, Limits, Project, UserPaths, save_config
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


def invoke(binary, paths, payload):
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
            "codex",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=2,
    )


def test_native_envelope_is_consumed_by_python(rust_adapter, native_setup):
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
    root = paths.project_data(project.id)
    incoming = list((root / "inbox").glob("*.json"))
    assert len(incoming) == 1 and b"private-value" not in incoming[0].read_bytes()
    envelope = Envelope.model_validate_json(incoming[0].read_bytes())
    assert envelope.session_id == "native" and envelope.kind == "turn.start"
    assert "explain code" in envelope.model_dump_json()
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
    incoming = list((paths.project_data(project.id) / "inbox").glob("*.json"))
    assert len(incoming) == 1 and b"omit this" not in incoming[0].read_bytes()
    set_desired(paths, paused=True)
    assert invoke(rust_adapter, paths, payload).stdout == "{}\n"
    assert len(list(incoming[0].parent.glob("*.json"))) == 1


def test_native_respects_python_control_lock(rust_adapter, native_setup):
    paths, project = native_setup
    with writer_lock(paths.data / "control.lock"):
        result = invoke(rust_adapter, paths, {"cwd": str(project.root), "hook_event_name": "Stop"})
    assert result.stdout == "{}\n"
    assert not list(paths.project_data(project.id).glob("inbox/*.json"))


def test_native_counts_expanded_unicode_payload(rust_adapter, native_setup):
    from agent_watchdog.resources import losses

    paths, project = native_setup
    save_config(paths.config, Config(defaults=Limits(payload_bytes=4096), projects=(project,)))
    payload = {
        "cwd": str(project.root),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "\u754c" * 600,
    }
    result = invoke(rust_adapter, paths, payload)
    assert result.stdout == "{}\n"
    assert not list(paths.project_data(project.id).glob("inbox/*.json"))
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
    incoming = next(paths.project_data(project.id).glob("inbox/*.json"))
    actual = Envelope.model_validate_json(incoming.read_bytes())
    exclude = {"received_at", "event_id"}
    assert actual.model_dump(exclude=exclude) == captured[0].model_dump(exclude=exclude)


@pytest.mark.parametrize("lock_name", ["admission.lock", "losses.lock"])
def test_native_respects_project_locks(rust_adapter, native_setup, lock_name):
    from agent_watchdog.resources import losses

    paths, project = native_setup
    root = paths.project_data(project.id)
    root.mkdir(parents=True)
    with writer_lock(root / lock_name):
        if lock_name == "losses.lock":
            with writer_lock(paths.data / "control.lock"):
                result = invoke(rust_adapter, paths, {"cwd": str(project.root)})
        else:
            result = invoke(rust_adapter, paths, {"cwd": str(project.root)})
    assert result.stdout == "{}\n" and not list(root.glob("inbox/*.json"))
    assert losses(root)["busy"] == (lock_name == "admission.lock")


def test_native_quota_and_concurrent_publication(rust_adapter, native_setup):
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
    root = paths.project_data(project.id)
    incoming = list(root.glob("inbox/*.json"))
    assert 0 < len(incoming) < 8
    assert usage(root / "inbox") <= limits.inbox_bytes
    assert len(incoming) + losses(root)["quota"] + losses(root)["busy"] == 8
    assert len({Envelope.model_validate_json(p.read_bytes()).event_id for p in incoming}) == len(
        incoming
    )
    assert not list(root.glob("inbox/*.tmp"))


def test_native_unregistered_and_invalid_config_fail_open(rust_adapter, native_setup, tmp_path):
    paths, project = native_setup
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    assert invoke(rust_adapter, paths, {"cwd": str(foreign)}).stdout == "{}\n"
    paths.config.write_text("schema_version = 999\n", encoding="utf-8")
    assert invoke(rust_adapter, paths, {"cwd": str(project.root)}).stdout == "{}\n"
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
