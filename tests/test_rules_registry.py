"""WD-142: rule discovery, derived approval status, and subscriptions."""

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from agent_watchdog.config import Config, Rules, UserPaths
from agent_watchdog.rules.registry import BUILTIN_NAMES, RuleRegistry

BODY = {
    "schema_version": 1,
    "name": "my_rule",
    "version": "1",
    "origin": "user",
    "provider": ["claude"],
    "event": "PreToolUse",
    "match": {"tool_name": "Bash", "input_regex": "danger"},
    "action": {"kind": "ask", "message": "Watchdog: careful."},
}


@pytest.fixture
def paths(tmp_path) -> UserPaths:
    return UserPaths(tmp_path / "cfg" / "config.toml", tmp_path / "data", tmp_path / "run")


def write_rule(paths: UserPaths, name: str = "my_rule", **overrides: object) -> Path:
    path = paths.config.parent / "rules" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({**BODY, "name": name, **overrides}), encoding="utf-8")
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entries(paths: UserPaths, **rules: Any) -> dict:
    config = Config(rules=Rules.model_validate(rules))
    return {entry.name: entry for entry in RuleRegistry(paths).entries(config)}


def test_the_four_builtins_ship_and_only_the_verification_stop_is_off(paths):
    found = entries(paths)

    assert set(BUILTIN_NAMES) == {
        "subagent_same_model",
        "destructive_command",
        "repeat_same_input_same_output",
        "stop_without_verification",
    }
    assert {name: found[name].status for name in BUILTIN_NAMES} == {
        "subagent_same_model": "approved",
        "destructive_command": "approved",
        "repeat_same_input_same_output": "approved",
        "stop_without_verification": "disabled",
    }
    assert all(found[name].source == "builtin" for name in BUILTIN_NAMES)


def test_enabling_the_default_off_builtin_activates_it_and_disabling_turns_another_off(paths):
    found = entries(paths, enabled=["stop_without_verification"], disabled=["destructive_command"])

    assert found["stop_without_verification"].status == "approved"
    assert found["destructive_command"].status == "disabled"
    assert found["subagent_same_model"].status == "approved"


def test_a_user_rule_is_proposed_until_its_hash_is_approved(paths):
    path = write_rule(paths)

    assert entries(paths)["my_rule"].status == "proposed"
    assert entries(paths, approved={"my_rule": digest(path)})["my_rule"].status == "approved"
    assert entries(paths, approved={"my_rule": "0" * 64})["my_rule"].status == "stale"


def test_editing_an_approved_rule_makes_it_stale_and_it_is_not_active(paths):
    path = write_rule(paths)
    approved = {"my_rule": digest(path)}
    registry = RuleRegistry(paths)
    config = Config(rules=Rules(approved=approved))
    assert [e.name for e in registry.entries(config) if e.active and e.source == "user"] == [
        "my_rule"
    ]

    write_rule(paths, action={"kind": "deny", "message": "Watchdog: now stricter."})
    os.utime(path, ns=(10**18, 10**18))

    found = {e.name: e for e in registry.entries(config)}
    assert found["my_rule"].status == "stale"
    assert not found["my_rule"].active


def test_a_disabled_user_rule_is_reported_disabled(paths):
    path = write_rule(paths)

    found = entries(paths, approved={"my_rule": digest(path)}, disabled=["my_rule"])

    assert found["my_rule"].status == "disabled"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not = [valid", id="bad-toml"),
        pytest.param(b"\xff\xfe\x00garbage", id="not-utf8"),
        pytest.param(tomli_w.dumps({**BODY, "state": {"nope": True}}).encode(), id="bad-predicate"),
        pytest.param(b"x" * (65 * 1024), id="too-large"),
    ],
)
def test_a_broken_rule_file_is_invalid_and_harms_nothing_else(paths, content):
    good = write_rule(paths, "good_rule")
    bad = paths.config.parent / "rules" / "broken.toml"
    bad.write_bytes(content)

    found = entries(paths, approved={"good_rule": digest(good)})

    assert found["broken"].status == "invalid" and found["broken"].error
    assert found["good_rule"].status == "approved"
    assert not found["broken"].active


def test_a_user_rule_cannot_shadow_a_builtin_or_misname_its_file(paths):
    write_rule(paths, "destructive_command")
    misnamed = paths.config.parent / "rules" / "other.toml"
    misnamed.write_text(tomli_w.dumps(BODY), encoding="utf-8")

    everything = RuleRegistry(paths).entries(Config())

    builtin = [e for e in everything if e.source == "builtin" and e.name == "destructive_command"]
    user = [e for e in everything if e.source == "user"]
    assert [e.status for e in builtin] == ["approved"]
    assert len(user) == 2 and {e.status for e in user} == {"invalid"}


def test_non_toml_files_and_subdirectories_are_ignored(paths):
    write_rule(paths)
    rules_dir = paths.config.parent / "rules"
    (rules_dir / "notes.txt").write_text("x")
    (rules_dir / "proposed").mkdir()
    (rules_dir / "proposed" / "later.toml").write_text(tomli_w.dumps({**BODY, "name": "later"}))

    names = {e.name for e in RuleRegistry(paths).entries(Config()) if e.source == "user"}

    assert names == {"my_rule"}


def test_entries_are_cached_until_a_file_or_the_rule_configuration_changes(paths):
    path = write_rule(paths)
    registry = RuleRegistry(paths)
    config = Config()
    first = registry.entries(config)

    assert registry.entries(config) is first
    assert registry.entries(Config(rules=Rules(disabled=["my_rule"]))) is not first

    path.write_text(tomli_w.dumps({**BODY, "version": "2"}), encoding="utf-8")
    os.utime(path, ns=(2 * 10**18, 2 * 10**18))
    changed = {e.name: e for e in registry.entries(config)}
    assert changed["my_rule"].rule is not None
    assert changed["my_rule"].rule.version == "2"


def test_a_missing_rules_directory_leaves_only_the_builtins(paths):
    assert {e.source for e in RuleRegistry(paths).entries(Config())} == {"builtin"}


def test_builtins_come_first_then_user_rules_by_priority(paths):
    write_rule(paths, "zeta", priority=1)
    write_rule(paths, "alpha", priority=50)
    config = Config(
        rules=Rules(
            approved={
                "zeta": digest(paths.config.parent / "rules" / "zeta.toml"),
                "alpha": digest(paths.config.parent / "rules" / "alpha.toml"),
            }
        )
    )

    active = [e.name for e in RuleRegistry(paths).active(config)]

    assert active[-2:] == ["zeta", "alpha"]
    assert set(active[:-2]) == set(BUILTIN_NAMES) - {"stop_without_verification"}


def test_subscriptions_follow_the_active_rules(paths):
    registry = RuleRegistry(paths)

    base = registry.subscriptions(Config())
    assert {"provider": "claude", "hook_event_name": "PreToolUse", "tool_name": "Agent"} in base
    assert {"provider": "claude", "hook_event_name": "PreToolUse", "tool_name": "Bash"} in base
    assert {"provider": "claude", "hook_event_name": "PostToolUse", "tool_name": None} in base
    assert not any(item["hook_event_name"] == "Stop" for item in base)

    enabled = registry.subscriptions(Config(rules=Rules(enabled=["stop_without_verification"])))
    assert {"provider": "claude", "hook_event_name": "Stop", "tool_name": None} in enabled

    nothing = registry.subscriptions(
        Config(
            rules=Rules(
                disabled=[
                    "subagent_same_model",
                    "destructive_command",
                    "repeat_same_input_same_output",
                ]
            )
        )
    )
    assert nothing == []


def test_a_newly_approved_rule_adds_its_subscription(paths):
    registry = RuleRegistry(paths)
    path = write_rule(
        paths, event="UserPromptSubmit", match={}, action={"kind": "context", "message": "m"}
    )
    before = registry.subscriptions(Config())

    after = registry.subscriptions(Config(rules=Rules(approved={"my_rule": digest(path)})))

    assert {"provider": "claude", "hook_event_name": "UserPromptSubmit", "tool_name": None} in after
    assert len(after) == len(before) + 1
