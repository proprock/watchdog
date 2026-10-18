"""Offline replay safety and causal session-state tests (WD-143/144)."""

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import Config, Limits, Project, Rules, UserPaths, load_config
from agent_watchdog.hooks import build_envelope
from agent_watchdog.registry import Resolution
from agent_watchdog.rules.manage import RulesError
from agent_watchdog.rules.replay import replay
from agent_watchdog.storage import Store

START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _environment(tmp_path: Path, *, approved: bool) -> tuple[UserPaths, Config, str, Path]:
    project_id = uuid4()
    root = tmp_path / "project"
    root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "run")
    rule_dir = paths.config.parent / "rules"
    rule_dir.mkdir(parents=True)
    marker = tmp_path / "executed"
    source = (
        "RULE = {'name': 'custom', 'version': '1', 'origin': 'user', "
        "'provider': ['claude'], 'event': 'PreToolUse', "
        "'action': {'kind': 'deny', 'message': 'blocked'}, 'timeout_ms': 1000}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "from agent_watchdog.rules.api import Decision\n"
        "def evaluate(ctx):\n"
        "    return Decision(action='deny', reason=str(ctx.session.calls if ctx.session else 0))\n"
    ).encode()
    rule_path = rule_dir / "custom.py"
    rule_path.write_bytes(source)
    approval = {"custom": hashlib.sha256(source).hexdigest()} if approved else {}
    config = Config(
        projects=(Project(id=project_id, root=root),),
        rules=Rules(approved=approval),
    )
    return paths, config, str(uuid4()), marker


def _save(paths: UserPaths, config: Config, session: str, hooks: list[dict]) -> None:
    project = config.projects[0]
    data_path = paths.project_data(project.id)
    data_path.mkdir(parents=True)
    resolution = Resolution(project.id, uuid4(), project.root)
    with Store(data_path, project.id) as store:
        for index, hook in enumerate(hooks):
            event = build_envelope(
                {"session_id": session, "cwd": str(project.root), **hook},
                resolution,
                Limits(),
                "claude",
                received_at=START + timedelta(seconds=index),
            )
            store.put(event)


def test_python_rule_must_be_approved_before_initialization_and_replay_stays_disabled(tmp_path):
    paths, config, session, marker = _environment(tmp_path, approved=False)
    _save(paths, config, session, [{"hook_event_name": "PreToolUse", "tool_name": "Bash"}])

    with pytest.raises(RulesError, match="approved"):
        replay(paths, config, "custom", session)
    assert not marker.exists()

    approved = config.model_copy(
        update={
            "rules": Rules(
                approved={
                    "custom": hashlib.sha256(
                        (paths.config.parent / "rules" / "custom.py").read_bytes()
                    ).hexdigest()
                }
            )
        }
    )
    result = replay(paths, approved, "custom", session)

    assert result["replay"] is True
    assert result["outcomes"][0]["decision"]["action"] == "deny"
    assert result["outcomes"][0]["decision"]["reason"] == "0"
    assert marker.exists()
    assert "custom" not in approved.rules.enabled


def test_replay_reconstructs_state_before_each_event(tmp_path):
    paths, config, session, _ = _environment(tmp_path, approved=True)
    _save(
        paths,
        config,
        session,
        [
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "one"},
            },
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "two"},
            },
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "three"},
            },
        ],
    )

    result = replay(paths, config, "custom", session)

    assert len(result["outcomes"]) == 3
    assert result["outcomes"][-1]["decision"]["reason"] == "2"
    assert all(item["decision"]["action"] == "allow" for item in result["outcomes"][:2])


def test_replay_rejects_ambiguous_session_across_projects(tmp_path):
    paths, config, session, _ = _environment(tmp_path, approved=True)
    _save(paths, config, session, [{"hook_event_name": "PreToolUse", "tool_name": "Bash"}])
    other_id = uuid4()
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = Project(id=other_id, root=other_root)
    ambiguous = config.model_copy(update={"projects": (*config.projects, other)})
    other_path = paths.project_data(other_id)
    other_path.mkdir(parents=True)
    resolution = Resolution(other_id, uuid4(), other_root)
    with Store(other_path, other_id) as store:
        store.put(
            build_envelope(
                {"session_id": session, "cwd": str(other_root), "hook_event_name": "PreToolUse"},
                resolution,
                Limits(),
                "claude",
            )
        )

    with pytest.raises(RulesError, match="ambiguous"):
        replay(paths, ambiguous, "custom", session)


def test_rules_test_cli_replays_session_as_json(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()

    def cli(*args: str) -> dict:
        monkeypatch.setattr(sys, "argv", ["agent-watchdog", "--home", str(home), *args])
        assert main() == 0
        return json.loads(capsys.readouterr().out)

    cli("project", "add", str(root))
    paths = UserPaths(home / "config.toml", home / "data", home / "runtime")
    rule_dir = paths.config.parent / "rules"
    rule_dir.mkdir()
    source = (
        b"RULE = {'name': 'custom', 'version': '1', 'origin': 'user', "
        b"'provider': ['claude'], 'event': 'PreToolUse', "
        b"'action': {'kind': 'deny', 'message': 'blocked'}, 'timeout_ms': 1000}\n"
        b"from agent_watchdog.rules.api import Decision\n"
        b"def evaluate(ctx):\n    return Decision(action='deny', reason='replayed')\n"
    )
    (rule_dir / "custom.py").write_bytes(source)
    cli("rules", "approve", "custom", "--yes")
    session = str(uuid4())
    _save(
        paths,
        load_config(paths.config),
        session,
        [{"hook_event_name": "PreToolUse", "tool_name": "Bash"}],
    )

    result = cli("rules", "test", "custom", "--session", session)

    assert result["replay"] is True
    assert result["outcomes"][0]["decision"]["reason"] == "replayed"
