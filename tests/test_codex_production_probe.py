"""Offline contracts for the isolated production-adapter probe."""

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_watchdog.config import load_config
from agent_watchdog.daemon import _policy_server, _SessionTracker
from agent_watchdog.storage import writer_lock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
try:
    production = importlib.import_module("codex_production_probe")
finally:
    sys.path.pop(0)


def test_production_configuration_approves_only_scratch_rule(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    log = tmp_path / "callbacks.ndjson"
    adapter = tmp_path / "adapter.exe"
    adapter.write_bytes(b"synthetic")
    paths, hooks = production.configure(tmp_path, work, log, adapter)

    config = load_config(paths.config)
    assert config.defaults.policy_intervene is True
    assert len(config.projects) == 1
    assert config.projects[0].root == work
    assert set(config.rules.approved) == {production.RULE_NAME}
    assert "--adapter" in hooks
    parsed = tomllib.loads(hooks)
    assert str(adapter) in parsed["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"]
    assert "[[hooks.PreToolUse]]" in hooks


def test_handler_relays_adapter_output_without_generating_control(monkeypatch, tmp_path, capsys):
    class Result:
        returncode = 0
        stdout = b'{"hookSpecificOutput":{"permissionDecision":"deny"}}\n'
        stderr = b""

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(production.subprocess, "run", fake_run)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch"}
    output = production.relay_adapter(
        payload,
        tmp_path / "adapter.exe",
        production.paths_at(tmp_path),
        tmp_path / "callbacks.ndjson",
    )
    assert output == Result.stdout
    assert calls[0][1]["input"] == json.dumps(payload).encode()
    assert calls[0][0][-2:] == ["hook", "codex"]
    records = [
        json.loads(line) for line in (tmp_path / "callbacks.ndjson").read_text().splitlines()
    ]
    assert records[0]["output"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("version", ["codex-cli 0.162.0-alpha.17.1", "codex-cli 0.163.0"])
def test_exact_provider_version_required(version):
    with pytest.raises(ValueError, match="requires Codex CLI"):
        production.require_provider_version(version)


def test_gap_or_unrelated_control_cannot_satisfy_production_readback():
    def control(action, delivered_at, ids, rule=production.RULE_NAME):
        return SimpleNamespace(
            kind="control",
            provider="codex",
            source="daemon",
            payload={
                "codex": {
                    "action": action,
                    "rule": rule,
                    "delivered_at": delivered_at,
                    "evidence_ids": ids,
                }
            },
        )

    event_id = "adapter-event"
    records = [
        control("log", None, [event_id]),
        control("deny", "2026-10-15T00:00:00Z", ["other-event"]),
        control("deny", None, [event_id]),
        control("deny", "2026-10-15T00:00:00Z", [event_id], rule="other_rule"),
    ]
    assert production.confirmed_controls(records, {event_id}) == 0
    records.append(control("deny", "2026-10-15T00:00:00Z", [event_id]))
    assert production.confirmed_controls(records, {event_id}) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="versioned Windows control profile")
def test_native_adapter_decision_is_read_back_from_scratch_store(rust_adapter, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    log = tmp_path / "callback.ndjson"
    paths, _ = production.configure(tmp_path, work, log, rust_adapter)
    payload = {
        "cwd": str(work),
        "session_id": "scratch-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "synthetic patch"},
    }
    with writer_lock(paths.data / "daemon.lock"), _policy_server(paths, _SessionTracker()):
        output = production.relay_adapter(payload, rust_adapter, paths, log)
    readback = production.readback(paths)
    rendered = json.loads(output)
    assert rendered["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert readback["spooled_patch_events"] == 1
    assert readback["persisted_controls"] == 1
