"""Offline contracts for the isolated WD-163 CLI runner."""

import argparse
import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
try:
    probe = importlib.import_module("codex_control_probe")
    live_probe = probe.probes
finally:
    sys.path.pop(0)


def test_isolated_environment_removes_parent_bridge_and_preserves_login_transport(tmp_path):
    env = probe.isolated_environment(
        tmp_path,
        {"PATH": "tools", "CODEX_THREAD_ID": "parent", "CODEX_APP_TOOLS_PIPE_PATH": "pipe"},
    )
    assert env == {"PATH": "tools", "CODEX_HOME": str(tmp_path)}


def test_readback_excludes_prompts_hook_payloads_and_tool_arguments():
    text = "\n".join(
        json.dumps(item)
        for item in [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "prompt-secret"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "arguments": "call-secret"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "model-reply"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "tool-result"},
            },
        ]
    )
    assert probe.observed_text(text) == "model-reply\ntool-result"


def test_interactive_cases_are_never_sent_to_exec(tmp_path):
    case = live_probe.CODEX_CASES["pre-compact-context"]
    with pytest.raises(ValueError, match="interactive"):
        probe.command_line("codex", case, tmp_path, interactive=False)
    command = probe.command_line("codex", case, tmp_path, interactive=True)
    assert "exec" not in command
    assert "--no-alt-screen" in command
    assert command[-1] == "Reply with exactly READY."


def test_code_mode_enables_the_selected_path_and_facts_exclude_untrusted_names(tmp_path):
    case = live_probe.CODEX_CASES["pre-tool-mcp-rewrite-code-mode"]
    command = probe.command_line("codex", case, tmp_path, interactive=False)
    assert "code_mode" in command and "code_mode_only" in command
    raw = "\n".join(
        json.dumps(item)
        for item in [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "secret"}},
            {"type": "turn_context", "payload": {"sandbox_policy": {"type": "workspace-write"}}},
        ]
    )
    assert probe.rollout_facts(raw) == {
        "observed_tool_paths": ["exec"],
        "effective_sandbox": "workspace-write",
    }


def test_observed_callback_counts_include_additional_provider_invocations():
    case = live_probe.CODEX_CASES["pre-tool-bash-context"]
    evidence = live_probe.CodexEvidence(
        records=[{"event": "PreToolUse", "input": {}}] * 2,
        stdout=live_probe.CODEX_CONTEXT_MARKER,
        rollout="",
        files=frozenset(),
        completed=True,
    )
    record = probe.result_record(
        case,
        evidence,
        version="test",
        started_at="2026-10-14T00:00:00Z",
        cleanup={"process": True, "profile": True, "scratch": True},
    )
    assert record["stages"]["callbacks"]["observed"] == 2
    assert record["stages"]["callbacks"]["expected"] == 2
    assert record["stages"]["callbacks"]["missing"] == 0
    assert record["scope"]["callback_minimums"] == {"PreToolUse": 1}


def test_ask_cannot_be_rejected_merely_because_an_operator_allowed_the_command():
    case = live_probe.CODEX_CASES["pre-tool-bash-ask"]
    evidence = live_probe.CodexEvidence(
        records=[{"event": "PreToolUse", "input": {}}],
        stdout=live_probe.CODEX_DENY_REASON,
        rollout="",
        files=frozenset({live_probe.CODEX_SENTINEL}),
        completed=True,
    )
    assert live_probe.evaluate_codex(case, evidence)[0] == "inconclusive"
    rejected = replace(evidence, stdout="unsupported permissionDecision:ask")
    assert live_probe.evaluate_codex(case, rejected)[0] == "unsupported"


@pytest.mark.parametrize(
    ("case_name", "event"),
    [("post-tool-bash-halt", "PostToolUse"), ("permission-bash-allow", "PermissionRequest")],
)
def test_missing_effect_after_a_callback_remains_inconclusive(case_name, event):
    evidence = live_probe.CodexEvidence(
        records=[{"event": event, "input": {}}],
        stdout="",
        rollout="",
        files=frozenset(),
        completed=True,
    )
    assert (
        live_probe.evaluate_codex(live_probe.CODEX_CASES[case_name], evidence)[0] == "inconclusive"
    )


def test_legacy_control_entry_point_uses_the_isolated_runner(monkeypatch):
    monkeypatch.setattr(probe.probes, "require_standalone_cli", lambda: None)
    called = []

    def fake_case(args, case):
        called.append(case.name)
        assert args.debug_log is None
        return {"isolated": True}

    monkeypatch.setattr(probe, "run_case", fake_case)
    args = argparse.Namespace(
        live=True,
        all=False,
        case=["pre-tool-bash-context"],
        codex="codex",
        interactive=False,
    )
    assert probe.probes.run_codex_control(args) == [{"isolated": True}]
    assert called == ["pre-tool-bash-context"]


@pytest.mark.parametrize("resource", ["process", "profile", "scratch"])
def test_cleanup_failure_cannot_enable_a_capability(resource):
    case = live_probe.CODEX_CASES["pre-tool-bash-context"]
    evidence = live_probe.CodexEvidence(
        records=[{"event": "PreToolUse", "input": {}}],
        stdout=live_probe.CODEX_CONTEXT_MARKER,
        rollout="",
        files=frozenset(),
        completed=True,
    )
    cleanup = {"process": True, "profile": True, "scratch": True}
    cleanup[resource] = False
    record = probe.result_record(
        case, evidence, version="test", started_at="2026-10-14T00:00:00Z", cleanup=cleanup
    )
    assert record["capabilities"][case.name]["state"] == "inconclusive"
    assert record["capabilities"][case.name]["observed_effect"] == "supported"
    assert record["result"]["state"] == "incomplete"
    assert record["cleanup"]["complete"] is False


def test_attempt_ids_are_unique_and_dispatch_missing_is_not_confirmed():
    case = live_probe.CODEX_CASES["pre-tool-bash-context"]
    evidence = live_probe.CodexEvidence(
        records=[], stdout="", rollout="", files=frozenset(), completed=True
    )
    kwargs = dict(
        version="test",
        started_at="2026-10-14T00:00:00Z",
        cleanup={"process": True, "profile": True, "scratch": True},
    )
    first = probe.result_record(case, evidence, **kwargs)
    second = probe.result_record(case, evidence, **kwargs)
    assert first["record_id"] != second["record_id"]
    assert first["stages"]["provider_dispatch"]["state"] == "missing"


def test_patch_rewrite_requires_actual_file_effect_not_model_claim():
    case = live_probe.CODEX_CASES["pre-tool-apply-patch-rewrite"]
    evidence = live_probe.CodexEvidence(
        records=[{"event": "PreToolUse", "input": {}}],
        stdout=live_probe.CODEX_REWRITTEN,
        rollout=live_probe.CODEX_REWRITTEN,
        files=frozenset(),
        completed=True,
    )
    assert live_probe.evaluate_codex(case, evidence)[0] == "inconclusive"
    assert (
        live_probe.evaluate_codex(
            case, replace(evidence, file_markers=frozenset({live_probe.CODEX_REWRITTEN}))
        )[0]
        == "supported"
    )


def test_live_flag_is_required_before_any_resource_is_created(monkeypatch):
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--case", "pre-tool-apply-patch-rewrite"])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows job accounting")
def test_cleanup_confirms_the_owned_process_and_its_child_are_stopped(tmp_path):
    code = (
        "import subprocess,sys,time; sys.stdin.readline(); "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); "
        "print('ready',flush=True); time.sleep(90)"
    )
    launcher = tmp_path / "launch.py"
    command = tmp_path / "command.json"
    gate = tmp_path / "ready"
    launcher.write_text(probe.LAUNCHER, encoding="utf-8")
    command.write_text(json.dumps([sys.executable, "-c", code]), encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(launcher),
            str(gate),
            str(command),
            str(probe.probes.hidden_creationflags()),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        creationflags=probe.probes.hidden_creationflags(),
    )
    job = probe.OwnedProcessJob(process)
    try:
        gate.write_text("ready", encoding="utf-8")
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("start\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "ready"
        assert probe.stop_job(job, process) is True
        assert process.poll() is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows runner preflight")
def test_auth_copy_failure_records_no_dispatch_and_cleans_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.probes, "resolve_codex", lambda _: "unused")
    monkeypatch.setattr(probe.probes, "codex_version", lambda _: "test")
    monkeypatch.setattr(probe.probes, "registered_roots", lambda _: [])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(probe.tempfile, "mkdtemp", lambda **_: str(scratch))

    def fail_copy(*_args):
        raise PermissionError("synthetic credential-copy failure")

    monkeypatch.setattr(probe.shutil, "copyfile", fail_copy)
    args = argparse.Namespace(codex="unused", config=None, interactive=False, debug_log=None)
    record = probe.run_case(args, live_probe.CODEX_CASES["pre-tool-bash-context"])
    assert record["stages"]["provider_dispatch"]["attempts"] == 0
    assert record["stages"]["provider_dispatch"]["state"] == "failed"
    assert record["cleanup"]["complete"] is True
    assert not scratch.exists()
