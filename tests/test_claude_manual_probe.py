"""Offline behavior for the operator-driven Claude CLI probe kit."""

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live_probe = _module("live_probe", ROOT / "scripts" / "live_probe.py")
manual_probe = _module("claude_manual_probe", ROOT / "scripts" / "claude_manual_probe.py")


def test_prepare_creates_one_case_project_local_hook_and_fixed_command(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "case-1"

    result = manual_probe.prepare(scratch, "pre-tool-ask")

    settings = json.loads((scratch / ".claude" / "settings.local.json").read_text())
    assert result["case"] == "pre-tool-ask"
    assert (scratch / ".git").is_dir()
    assert set(settings["hooks"]) == {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
    }
    commands = {
        entry["hooks"][0]["command"] for entries in settings["hooks"].values() for entry in entries
    }
    assert len(commands) == 1
    command = commands.pop()
    assert "handler" in command and "--case pre-tool-ask" in command
    assert scratch.as_posix() in command
    assert settings["permissions"]["allow"] == [manual_probe.ALLOWLIST]
    manifest = json.loads((scratch / manual_probe.MANIFEST_RELATIVE).read_text())
    assert (
        manifest["settings_sha256"]
        == hashlib.sha256((scratch / ".claude" / "settings.local.json").read_bytes()).hexdigest()
    )
    assert "global" not in command.lower()
    assert "WD139_CTX_" not in result["steps"][2]
    assert live_probe.DENY_REASON not in result["steps"][2]


def test_prepare_notification_has_observation_callback_without_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])

    manual_probe.prepare(tmp_path / "notification", "notification")

    settings = json.loads(
        (tmp_path / "notification" / ".claude" / "settings.local.json").read_text()
    )
    assert set(settings["hooks"]) == {"UserPromptSubmit", "Notification"}
    assert "control response is claimed" in manual_probe.CASES["notification"].response_shape
    for case in ("notification-system-message", "notification-continue"):
        assert "WD153_NOTIFICATION_CONTEXT" not in manual_probe.CASES[case].prompt
        assert "READY" not in manual_probe.CASES[case].prompt


def test_baseline_and_ask_share_workload_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    baseline = manual_probe.prepare(tmp_path / "baseline", "baseline")
    ask = manual_probe.prepare(tmp_path / "ask", "pre-tool-ask")
    baseline_manifest = json.loads(
        (tmp_path / "baseline" / manual_probe.MANIFEST_RELATIVE).read_text()
    )
    ask_manifest = json.loads((tmp_path / "ask" / manual_probe.MANIFEST_RELATIVE).read_text())
    baseline_settings = json.loads(
        (tmp_path / "baseline" / ".claude" / "settings.local.json").read_text()
    )
    ask_settings = json.loads((tmp_path / "ask" / ".claude" / "settings.local.json").read_text())

    assert baseline["steps"][2] == ask["steps"][2]
    assert baseline_manifest["scope_binding"] == ask_manifest["scope_binding"]
    assert baseline_settings["permissions"] == ask_settings["permissions"]
    assert "PermissionRequest" in baseline_settings["hooks"]
    assert "PermissionRequest" in ask_settings["hooks"]


def test_permission_pair_ignores_callbacks_for_other_commands():
    evidence = manual_probe.ManualEvidence(
        records=[
            {
                "event": event,
                "input": {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo OTHER > other.txt"},
                },
            }
            for event in ("PreToolUse", "PostToolUse", "PermissionRequest")
        ],
        stream=[],
        files=frozenset({live_probe.SENTINEL}),
    )

    verdict = manual_probe._verdict(manual_probe.CASES["baseline"], evidence, None, None)

    assert verdict[0] == "inconclusive"
    assert manual_probe._fixed_workload(
        {"tool_name": "Bash", "tool_input": {"command": manual_probe.WRITE_COMMAND + " \n"}},
        manual_probe.WRITE_COMMAND,
    )
    assert not manual_probe._fixed_workload(
        {
            "tool_name": "Bash",
            "tool_input": {"command": manual_probe.WRITE_COMMAND + " && echo extra"},
        },
        manual_probe.WRITE_COMMAND,
    )


def test_prepare_refuses_nonempty_and_registered_scratch(tmp_path, monkeypatch):
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep")
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])

    with pytest.raises(ValueError, match="new or empty"):
        manual_probe.prepare(nonempty, "baseline")

    registered = tmp_path / "registered"
    registered.mkdir()
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [registered])
    with pytest.raises(ValueError, match="inside a registered"):
        manual_probe.prepare(registered / "scratch", "baseline")


def test_prepare_refuses_reparse_path(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="reparse point"):
        manual_probe.prepare(link / "scratch", "baseline")


def _completed_transcript(path: Path, *, denied: bool = True, session: str = "bound") -> None:
    entries = [
        {"type": "user", "sessionId": session, "message": {"content": "the user prompt"}},
        {
            "type": "user",
            "sessionId": session,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "User denied permission" if denied else "tool completed",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "sessionId": session,
            "message": {
                "model": "claude-haiku-test",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "The command was refused."}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")


def _write_hook_log(scratch: Path, transcript: Path, session: str, events: list[str]) -> None:
    rows = [
        {
            "event": event,
            "input": {
                "session_id": session,
                "transcript_path": str(transcript),
                "cwd": str(scratch.resolve()),
                **(
                    {
                        "tool_name": "Bash",
                        "tool_input": {
                            "command": manual_probe.WRITE_COMMAND,
                        },
                    }
                    if event in {"PreToolUse", "PostToolUse", "PermissionRequest"}
                    else {}
                ),
            },
            "output": {},
        }
        for event in events
    ]
    (scratch / manual_probe.LOG_RELATIVE).write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )


def test_collect_binds_callbacks_to_session_and_requires_transcript_deny_readback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "ask"
    manual_probe.prepare(scratch, "pre-tool-ask")
    transcript = tmp_path / "session.jsonl"
    _completed_transcript(transcript)
    log = scratch / manual_probe.LOG_RELATIVE
    log.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "event": "UserPromptSubmit",
                    "input": {
                        "session_id": "bound",
                        "transcript_path": str(transcript),
                        "cwd": str(scratch.resolve()),
                    },
                },
                {
                    "event": "PreToolUse",
                    "input": {
                        "session_id": "bound",
                        "transcript_path": str(transcript),
                        "cwd": str(scratch.resolve()),
                        "tool_name": "Bash",
                        "tool_input": {"command": manual_probe.WRITE_COMMAND},
                    },
                },
                {
                    "event": "PermissionRequest",
                    "input": {
                        "session_id": "other",
                        "transcript_path": str(tmp_path / "missing"),
                        "cwd": str(scratch.resolve()),
                        "tool_name": "Bash",
                        "tool_input": {"command": manual_probe.WRITE_COMMAND},
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    (scratch / live_probe.SENTINEL).unlink(missing_ok=True)

    result = manual_probe.collect(
        scratch, version="2.1.300", operator_dialog="denied", disable_hook=True
    )

    record = result["record"]
    assert result["observed_session_bound"] is True
    assert result["terminal_completion"] == "confirmed"
    assert result["hook_disabled"] is True
    assert record["provenance"]["provider"]["version"] == "2.1.300"
    assert record["capabilities"]["pre-tool-ask"]["state"] == "inconclusive"
    assert record["capabilities"]["pre-tool-ask"]["callbacks"]["PermissionRequest"]["observed"] == 0
    assert "the user prompt" not in json.dumps(record)
    assert "baseline record is required" in record["result"]["limitations"][0]
    assert not (scratch / ".claude" / "settings.local.json").exists()
    assert (scratch / ".wd153" / "probe.ndjson").is_file()


def test_matching_supported_baseline_is_required_and_allows_ask_classification(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    baseline_dir = tmp_path / "baseline"
    manual_probe.prepare(baseline_dir, "baseline")
    baseline_transcript = tmp_path / "baseline-session.jsonl"
    _completed_transcript(baseline_transcript, denied=False, session="baseline-session")
    _write_hook_log(
        baseline_dir,
        baseline_transcript,
        "baseline-session",
        ["UserPromptSubmit", "PreToolUse", "PostToolUse"],
    )
    (baseline_dir / live_probe.SENTINEL).write_text("WD139_PERMISSION", encoding="utf-8")
    baseline = manual_probe.collect(baseline_dir, version="2.1.300")["record"]
    baseline_path = baseline_dir / ".wd153" / "probe-result.v1.json"

    ask_dir = tmp_path / "ask"
    manual_probe.prepare(ask_dir, "pre-tool-ask")
    ask_transcript = tmp_path / "ask-session.jsonl"
    _completed_transcript(ask_transcript, session="ask-session")
    _write_hook_log(
        ask_dir,
        ask_transcript,
        "ask-session",
        ["UserPromptSubmit", "PreToolUse", "PermissionRequest"],
    )
    assert (
        baseline["scope"]["wd153"]
        == json.loads((ask_dir / manual_probe.MANIFEST_RELATIVE).read_text())["scope_binding"]
    )

    record = manual_probe.collect(
        ask_dir,
        version="2.1.300",
        operator_dialog="denied",
        baseline=baseline_path,
    )["record"]

    assert record["capabilities"]["pre-tool-ask"]["state"] == "supported"
    assert record["capabilities"]["pre-tool-ask"]["callbacks"]["PermissionRequest"]["observed"] == 1
    assert record["scope"]["baseline_record_id"] == baseline["record_id"]


def test_modified_settings_are_preserved_and_make_collection_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "baseline"
    manual_probe.prepare(scratch, "baseline")
    transcript = tmp_path / "session.jsonl"
    _completed_transcript(transcript, denied=False, session="s1")
    _write_hook_log(scratch, transcript, "s1", ["UserPromptSubmit", "PreToolUse", "PostToolUse"])
    (scratch / live_probe.SENTINEL).write_text("WD139_PERMISSION", encoding="utf-8")
    settings = scratch / ".claude" / "settings.local.json"
    settings.write_text(settings.read_text(encoding="utf-8") + " ", encoding="utf-8")
    modified = settings.read_bytes()

    record = manual_probe.collect(scratch, version="2.1.300", disable_hook=True)["record"]

    assert settings.read_bytes() == modified
    assert record["capabilities"]["baseline"]["state"] == "inconclusive"
    resource = record["cleanup"]["resources"][0]
    assert resource["action"] == "retained"
    assert resource["state"] == "unknown"
    assert "settings changed" in record["result"]["limitations"][-1]


@pytest.mark.parametrize(
    "defect", ["duplicate-submit", "transcript-session", "cwd", "old-transcript"]
)
def test_collection_integrity_defects_never_claim_support(tmp_path, monkeypatch, defect):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "baseline"
    manual_probe.prepare(scratch, "baseline")
    transcript = tmp_path / "session.jsonl"
    _completed_transcript(
        transcript,
        denied=False,
        session="elsewhere" if defect == "transcript-session" else "s1",
    )
    events = ["UserPromptSubmit", "PreToolUse", "PostToolUse"]
    _write_hook_log(scratch, transcript, "s1", events)
    log = scratch / manual_probe.LOG_RELATIVE
    if defect == "duplicate-submit":
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        log.write_text("\n".join(json.dumps(row) for row in [*rows, rows[0]]), encoding="utf-8")
    elif defect == "cwd":
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        rows[1]["input"]["cwd"] = str(tmp_path / "elsewhere")
        log.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    elif defect == "old-transcript":
        import os

        os.utime(transcript, (1, 1))
    (scratch / live_probe.SENTINEL).write_text("WD139_PERMISSION", encoding="utf-8")

    record = manual_probe.collect(scratch, version="2.1.300")["record"]

    assert record["capabilities"]["baseline"]["state"] == "inconclusive"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("notification-system-message", {"systemMessage"}),
        ("notification-continue", {"continue"}),
    ],
)
def test_notification_control_attempts_are_one_field_per_case(
    tmp_path, monkeypatch, capsys, case, expected
):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / case
    manual_probe.prepare(scratch, case)
    monkeypatch.setattr(
        manual_probe,
        "_read_hook_input",
        lambda: ({"session_id": "s1", "cwd": str(scratch.resolve())}, "Notification"),
    )

    assert manual_probe.handle(scratch, case) == 0

    response = json.loads(capsys.readouterr().out)
    assert set(response) == expected
    assert len(response) == 1


@pytest.mark.parametrize(
    ("payload", "event"),
    [
        (
            {"cwd": "C:/elsewhere", "prompt": manual_probe.CASES["pre-tool-ask"].prompt},
            "UserPromptSubmit",
        ),
        (
            {"cwd": "C:/scratch", "tool_name": "Bash", "tool_input": {"command": "echo OTHER"}},
            "PreToolUse",
        ),
        (
            {"cwd": "C:/scratch", "tool_name": "Read", "tool_input": {"file_path": "x"}},
            "PermissionRequest",
        ),
    ],
)
def test_handler_fails_open_outside_case_cwd_or_for_unrelated_tools(
    tmp_path, monkeypatch, capsys, payload, event
):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "case"
    manual_probe.prepare(scratch, "pre-tool-ask")
    if payload["cwd"] == "C:/scratch":
        payload["cwd"] = str(scratch.resolve())
    monkeypatch.setattr(manual_probe, "_read_hook_input", lambda: (payload, event))

    assert manual_probe.handle(scratch, "pre-tool-ask") == 0

    assert json.loads(capsys.readouterr().out) == {}
    assert not (scratch / manual_probe.LOG_RELATIVE).exists()


@pytest.mark.parametrize(
    ("case", "behavior"),
    [("permission-allow", "allow"), ("permission-deny", "deny")],
)
def test_handler_emits_and_logs_case_specific_permission_response(
    tmp_path, monkeypatch, capsys, case, behavior
):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / case
    manual_probe.prepare(scratch, case)
    payload = {
        "cwd": str(scratch.resolve()),
        "tool_name": "Bash",
        "tool_input": {"command": manual_probe.CASES[case].tool_command},
    }
    monkeypatch.setattr(manual_probe, "_read_hook_input", lambda: (payload, "PermissionRequest"))

    assert manual_probe.handle(scratch, case) == 0

    response = json.loads(capsys.readouterr().out)
    decision = response["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == behavior
    log = [
        json.loads(line) for line in (scratch / manual_probe.LOG_RELATIVE).read_text().splitlines()
    ]
    assert len(log) == 1
    assert log[0]["event"] == "PermissionRequest"
    assert log[0]["input"]["tool_input"]["command"] == manual_probe.CASES[case].tool_command


@pytest.mark.parametrize(
    ("case", "output", "reply", "expected"),
    [
        (
            "notification-system-message",
            {"systemMessage": "After dismissal, reply exactly WD153_NOTIFICATION_CONTEXT."},
            "WD153_NOTIFICATION_CONTEXT",
            "supported",
        ),
        (
            "notification-continue",
            {"continue": False},
            "The scratch command completed.",
            "unsupported",
        ),
    ],
)
def test_notification_verdict_requires_same_session_transcript_effect_or_countereffect(
    case, output, reply, expected
):
    evidence = manual_probe.ManualEvidence(
        records=[{"event": "Notification", "input": {"session_id": "same"}, "output": output}],
        stream=[],
        files=frozenset(),
    )
    evidence.terminal = {
        "type": "assistant",
        "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": reply}]},
    }

    verdict = manual_probe._verdict(manual_probe.CASES[case], evidence, None, None)

    assert verdict[0] == expected


def test_collect_requires_real_terminal_completion_and_explicit_version(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "notification"
    manual_probe.prepare(scratch, "notification")
    transcript = tmp_path / "unfinished.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": "not terminal"}}),
        encoding="utf-8",
    )
    (scratch / manual_probe.LOG_RELATIVE).write_text(
        json.dumps(
            {
                "event": "UserPromptSubmit",
                "input": {"session_id": "s1", "transcript_path": str(transcript)},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit Claude CLI version"):
        manual_probe.collect(scratch, version="unknown")
    result = manual_probe.collect(scratch, version="2.1.300", disable_hook=True)
    assert result["terminal_completion"] == "not_confirmed"
    assert result["record"]["result"]["state"] == "incomplete"
    assert result["record"]["capabilities"]["notification"]["state"] == "inconclusive"
    assert "terminal assistant completion" in result["record"]["result"]["limitations"][-1]
    assert result["hook_disabled"] is True


def test_missing_terminal_completion_cannot_support_callback_and_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "permission-allow"
    manual_probe.prepare(scratch, "permission-allow")
    transcript = tmp_path / "rejected-tool-use.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "session-1",
                "cwd": str(scratch.resolve()),
                "message": {
                    "model": "claude-sonnet-test",
                    "stop_reason": "tool_use",
                    "content": [],
                },
            }
        ),
        encoding="utf-8",
    )
    events = ["UserPromptSubmit", "PermissionRequest"]
    rows = [
        {
            "event": event,
            "input": {
                "session_id": "session-1",
                "transcript_path": str(transcript),
                "cwd": str(scratch.resolve()),
                **(
                    {
                        "tool_name": "Bash",
                        "tool_input": {
                            "command": manual_probe.CASES["permission-allow"].tool_command
                        },
                    }
                    if event == "PermissionRequest"
                    else {}
                ),
            },
            "output": {},
        }
        for event in events
    ]
    (scratch / manual_probe.LOG_RELATIVE).write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    (scratch / live_probe.SENTINEL).write_text("present", encoding="utf-8")

    result = manual_probe.collect(scratch, version="2.1.292", disable_hook=True)

    record = result["record"]
    assert result["terminal_completion"] == "not_confirmed"
    assert result["hook_disabled"] is True
    assert record["capabilities"]["permission-allow"]["state"] == "inconclusive"
    assert record["stages"]["provider_dispatch"]["state"] == "incomplete"
    assert record["stages"]["provider_dispatch"]["detail"] == (
        "same-session transcript has no real terminal assistant completion event"
    )


def test_manual_metadata_normalization_corrects_headless_attribution_only():
    record = {
        "record_id": "wd153-claude-manual-baseline-runid",
        "started_at": "start",
        "finished_at": "finish",
        "provenance": {
            "provider": {"version": "2.1.292"},
            "source": {"kind": "manual-observation", "sanitized_reference": "wd153-baseline"},
        },
        "scope": {"isolation": "scratch and --settings", "notes": "session-bound"},
        "capabilities": {"baseline": {"state": "inconclusive", "models": ["claude-sonnet-5-5"]}},
        "cleanup": {
            "resources": [
                {"detail": "Passed with --settings; user and project settings were not loaded."}
            ],
            "complete": False,
        },
        "result": {
            "state": "incomplete",
            "limitations": [
                "One Windows Claude Code CLI version, haiku main model, one prompt.",
                "Watchdog persistence was not exercised.",
            ],
        },
        "evidence": {"prompt": "private prompt", "session_id": "private-session"},
    }
    original = deepcopy(record)

    normalized = manual_probe.normalize_manual_record_metadata(record)

    assert normalized["scope"]["isolation"] == (
        "Scratch Git repository outside registered projects with project-local "
        ".claude/settings.local.json; ordinary user and project settings may also load."
    )
    assert normalized["cleanup"]["resources"][0]["detail"] == (
        "Project-local .claude/settings.local.json in scratch; CLI launched with "
        "ordinary settings discovery."
    )
    assert any(
        "operator-reported from `claude --version`, not transcript-verified" in limitation
        for limitation in normalized["result"]["limitations"]
    )
    assert "haiku" not in " ".join(normalized["result"]["limitations"])
    assert normalized["capabilities"] == original["capabilities"]
    assert normalized["evidence"] == original["evidence"]
    assert normalized["record_id"] == original["record_id"]
    assert normalized["started_at"] == original["started_at"]
    assert normalized["finished_at"] == original["finished_at"]


def test_absent_notification_callback_is_never_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    scratch = tmp_path / "notification"
    manual_probe.prepare(scratch, "notification")
    transcript = tmp_path / "finished.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "message": {"stop_reason": "end_turn", "content": "done"},
            }
        ),
        encoding="utf-8",
    )
    (scratch / manual_probe.LOG_RELATIVE).write_text(
        json.dumps(
            {
                "event": "UserPromptSubmit",
                "input": {"session_id": "s1", "transcript_path": str(transcript)},
            }
        ),
        encoding="utf-8",
    )

    record = manual_probe.collect(scratch, version="2.1.300")["record"]

    assert record["capabilities"]["notification"]["state"] == "inconclusive"
    assert "no Notification callback" in record["result"]["limitations"][0]
    assert (scratch / ".claude" / "settings.local.json").is_file()
    with pytest.raises(ValueError, match="already exists"):
        manual_probe.collect(scratch, version="2.1.300")


def test_record_ids_are_unique_for_same_case_and_day(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_probe, "registered_roots", lambda _config: [])
    ids = []
    for index in range(2):
        scratch = tmp_path / f"notification-{index}"
        manual_probe.prepare(scratch, "notification")
        transcript = tmp_path / f"notification-{index}.jsonl"
        _completed_transcript(transcript, session=f"session-{index}")
        _write_hook_log(scratch, transcript, f"session-{index}", ["UserPromptSubmit"])
        ids.append(manual_probe.collect(scratch, version="2.1.300")["record"]["record_id"])

    assert ids[0] != ids[1]
