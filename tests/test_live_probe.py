"""Offline contracts for the opt-in Codex live-hook probe."""

import importlib.util
import io
import json
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest


def _live_probe():
    path = Path(__file__).parents[1] / "scripts" / "live_probe.py"
    spec = importlib.util.spec_from_file_location("live_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live_probe = _live_probe()


@pytest.mark.parametrize(
    ("event", "marker"),
    [
        ("UserPromptSubmit", live_probe.USER_PROMPT_MARKER),
        ("PostToolUse", live_probe.POST_TOOL_MARKER),
    ],
)
def test_codex_hook_response_uses_context_envelope(event, marker):
    assert live_probe.hook_response(event) == {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": marker,
        }
    }


def test_handler_records_the_scratch_callback_and_emits_the_exact_envelope(
    monkeypatch, capsys, tmp_path
):
    log = tmp_path / "probe.ndjson"
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "synthetic"}
    monkeypatch.setattr(
        live_probe.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )

    assert live_probe.handle(log) == 0

    assert json.loads(capsys.readouterr().out) == live_probe.hook_response("UserPromptSubmit")
    assert json.loads(log.read_text(encoding="utf-8")) == {
        "event": "UserPromptSubmit",
        "input": payload,
        "output": live_probe.hook_response("UserPromptSubmit"),
    }


def test_profile_loads_both_hooks_without_requiring_scratch_project_trust(tmp_path):
    profile = live_probe.profile_configuration(Path(r"C:\probe.py"), tmp_path / "probe.ndjson")

    parsed = tomllib.loads(profile)
    assert "[[hooks.UserPromptSubmit]]" in profile
    assert "[[hooks.PostToolUse]]" in profile
    assert 'matcher = "^Bash$"' in profile
    assert "commandWindows" in profile
    assert parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["type"] == "command"


def test_codex_command_selects_the_temporary_profile_and_persists_a_rollout(tmp_path):
    command = live_probe.codex_command("codex", "watchdog-wd139-test", tmp_path, "probe prompt")

    assert command[0:6] == [
        "codex",
        "exec",
        "--enable",
        "hooks",
        "--profile",
        "watchdog-wd139-test",
    ]
    assert "--dangerously-bypass-hook-trust" in command
    assert "--ephemeral" not in command


def test_inside_registered_root_detects_scratch_descendant(tmp_path):
    registered = tmp_path / "registered"
    scratch = registered / "probe-scratch"
    scratch.mkdir(parents=True)

    assert live_probe.inside_registered_root(scratch, [registered]) is True

    with pytest.raises(ValueError, match="inside a registered"):
        live_probe.ensure_scratch_outside_roots(scratch, [registered])


def test_outside_registered_roots_is_not_refused(tmp_path):
    registered = tmp_path / "registered"
    scratch = tmp_path / "scratch"
    registered.mkdir()
    scratch.mkdir()

    assert live_probe.inside_registered_root(scratch, [registered]) is False


@pytest.mark.parametrize(
    ("records", "stdout", "rollout", "missing"),
    [
        ([], "", "", "callback"),
        (
            [
                {
                    "event": "UserPromptSubmit",
                    "input": {},
                    "output": live_probe.hook_response("UserPromptSubmit"),
                }
            ],
            "",
            "",
            "PostToolUse",
        ),
    ],
)
def test_verify_reports_inconclusive_for_missing_evidence(records, stdout, rollout, missing):
    state, reasons = live_probe.verify(records, stdout, rollout)

    assert state == "inconclusive"
    assert any(missing.casefold() in reason.casefold() for reason in reasons)


def test_verify_reports_supported_when_both_events_and_markers_are_present():
    records = [
        {
            "event": event,
            "input": {},
            "output": live_probe.hook_response(event),
        }
        for event in ("UserPromptSubmit", "PostToolUse")
    ]
    evidence = f"{live_probe.USER_PROMPT_MARKER}\n{live_probe.POST_TOOL_MARKER}"

    assert live_probe.verify(records, evidence, evidence) == ("supported", [])


def test_result_keeps_only_callback_shapes_not_raw_hook_values():
    result = live_probe._result(
        version="codex-cli test",
        records=[
            {
                "event": "UserPromptSubmit",
                "input": {"hook_event_name": "UserPromptSubmit", "prompt": "secret"},
                "output": live_probe.hook_response("UserPromptSubmit"),
            }
        ],
        state="inconclusive",
        limitations=["missing PostToolUse"],
        exit_code=0,
        started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T00:01:00Z",
    )

    assert "secret" not in json.dumps(result)
    assert result["started_at"] == "2026-10-06T00:00:00Z"
    assert result["finished_at"] == "2026-10-06T00:01:00Z"
    assert result["cleanup"] == {
        "scratch_repository": "removed",
        "temporary_profile": "removed",
        "user_configuration": "unchanged",
    }
    assert result["callbacks"]["UserPromptSubmit"] == {
        "observed": 1,
        "input_fields": ["hook_event_name", "prompt"],
        "response": live_probe.hook_response("UserPromptSubmit"),
    }


def test_offline_helpers_never_launch_a_probe_subprocess(monkeypatch, tmp_path):
    forbidden = Mock(side_effect=AssertionError("offline helper launched a subprocess"))
    monkeypatch.setattr(live_probe.subprocess, "run", forbidden)
    monkeypatch.setattr(live_probe.subprocess, "Popen", forbidden)
    root = tmp_path / "registered"
    root.mkdir()
    records = [
        {
            "event": "UserPromptSubmit",
            "input": {},
            "output": live_probe.hook_response("UserPromptSubmit"),
        },
        {
            "event": "PostToolUse",
            "input": {},
            "output": live_probe.hook_response("PostToolUse"),
        },
    ]
    evidence = f"{live_probe.USER_PROMPT_MARKER} {live_probe.POST_TOOL_MARKER}"

    assert live_probe.verify(records, evidence, evidence)[0] == "supported"
    assert live_probe.inside_registered_root(Path(root), [root]) is True
    forbidden.assert_not_called()
