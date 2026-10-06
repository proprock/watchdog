"""Offline contracts for the opt-in Codex live-hook probe."""

import importlib.util
import io
import json
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest


def _live_probe():
    path = Path(__file__).parents[1] / "scripts" / "live_probe.py"
    spec = importlib.util.spec_from_file_location("live_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
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


def _stream(reply="", *, tool_results=(), models=(), error=False, compacted=False):
    events = []
    if compacted:
        events.append({"type": "system", "subtype": "compact_boundary"})
    for text in tool_results:
        events.append(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": text}]},
            }
        )
    events.append(
        {
            "type": "result",
            "result": reply,
            "is_error": error,
            "terminal_reason": "api_error" if error else "completed",
            "modelUsage": {model: {} for model in models},
        }
    )
    return events


def _calls(**counts):
    records = []
    for event, count in counts.items():
        records += [{"event": event, "input": {"hook_event_name": event}}] * count
    return records


def _evidence(records, stream, **fields):
    return live_probe.ClaudeEvidence(records=records, stream=stream, **fields)


def _case(name):
    return live_probe.CLAUDE_CASES[name]


def test_claude_handler_answers_with_the_case_response_and_logs_the_callback(
    monkeypatch, capsys, tmp_path
):
    log = tmp_path / "probe.ndjson"
    payload = {"hook_event_name": "PreToolUse", "tool_input": {"command": "x", "timeout": 5}}
    monkeypatch.setattr(
        live_probe.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )

    assert live_probe.handle_claude(log, "pre-tool-rewrite") == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["hookSpecificOutput"]["permissionDecision"] == "allow"
    # The rewrite replaces only the command; other fields of the call survive.
    assert printed["hookSpecificOutput"]["updatedInput"] == {
        "command": f"echo {live_probe.REWRITTEN}",
        "timeout": 5,
    }
    assert json.loads(log.read_text(encoding="utf-8"))["input"] == payload


def test_claude_handler_fails_open_with_an_empty_object(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(live_probe.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))

    assert live_probe.handle_claude(tmp_path / "probe.ndjson", "stop-block") == 0

    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize(("active", "blocks"), [(False, True), (True, False)])
def test_stop_block_never_blocks_a_stop_it_already_continued(active, blocks):
    response = _case("stop-block").responses["Stop"]({"stop_hook_active": active})

    assert (response.get("decision") == "block") is blocks


@pytest.mark.parametrize(
    ("name", "event", "expected"),
    [
        ("pre-tool-deny", "PreToolUse", ("deny", live_probe.DENY_REASON)),
        ("permission-allow", "PermissionRequest", ("allow", None)),
        ("permission-deny", "PermissionRequest", ("deny", live_probe.DENY_REASON)),
    ],
)
def test_decision_envelopes_match_the_documented_shape(name, event, expected):
    output = _case(name).responses[event]({"tool_input": {}})["hookSpecificOutput"]

    assert output["hookEventName"] == event
    decision = output.get("permissionDecision") or output["decision"]["behavior"]
    assert decision == expected[0]
    if expected[1] is not None:
        assert expected[1] in json.dumps(output)


def test_agent_rewrite_changes_only_the_model():
    original = {"subagent_type": "general-purpose", "model": "haiku", "prompt": "ok"}

    output = _case("agent-rewrite").responses["PreToolUse"]({"tool_input": original})

    assert output["hookSpecificOutput"]["updatedInput"] == {**original, "model": "sonnet"}


def test_claude_settings_hold_only_the_case_hooks_with_posix_paths(tmp_path):
    case = _case("agent-deny")

    settings = live_probe.claude_settings(case, Path(r"C:\probe\live_probe.py"), tmp_path / "p.log")

    assert set(settings["hooks"]) == {"PreToolUse", "SubagentStart"}
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == live_probe.AGENT_MATCHER
    assert "matcher" not in settings["hooks"]["SubagentStart"][0]
    command = entry["hooks"][0]["command"]
    assert "claude-handler" in command and "--case agent-deny" in command
    assert "\\" not in command
    json.dumps(settings)


def test_claude_command_isolates_settings_but_keeps_hooks_enabled(tmp_path):
    command = live_probe.claude_command("claude", _case("pre-tool-deny"), tmp_path / "s.json", 0.5)

    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--settings") + 1] == str(tmp_path / "s.json")
    assert "--safe-mode" not in command and "--bare" not in command
    assert "--input-format" not in command
    assert "--allowedTools" in command


def test_compaction_cases_send_history_as_stream_json_messages(tmp_path):
    case = _case("pre-compact-block")

    command = live_probe.claude_command("claude", case, tmp_path / "s.json", 0.5)
    messages = [json.loads(line) for line in live_probe.claude_stdin(case).splitlines()]

    assert command[command.index("--input-format") + 1] == "stream-json"
    assert [m["message"]["content"] for m in messages][-1] == "/compact"


@pytest.mark.parametrize(
    ("name", "records", "stream", "state"),
    [
        (
            "pre-tool-context",
            _calls(PreToolUse=1),
            _stream(f"{live_probe.CTX}PRE_TOOL"),
            "supported",
        ),
        ("pre-tool-context", _calls(PreToolUse=1), _stream("NONE"), "unsupported"),
        ("pre-tool-context", _calls(PreToolUse=1), _stream("unclear"), "inconclusive"),
        ("pre-tool-context", _calls(), _stream("NONE"), "inconclusive"),
        ("pre-tool-context", _calls(PreToolUse=1), _stream("NONE", error=True), "inconclusive"),
        (
            "pre-tool-deny",
            _calls(PreToolUse=1),
            _stream("refused", tool_results=[live_probe.DENY_REASON]),
            "supported",
        ),
        ("pre-tool-deny", _calls(PreToolUse=1, PostToolUse=1), _stream("ran"), "unsupported"),
        ("pre-tool-deny", _calls(PreToolUse=1), _stream("nothing"), "inconclusive"),
        (
            "pre-tool-rewrite",
            _calls(PreToolUse=1),
            _stream("", tool_results=[live_probe.REWRITTEN]),
            "supported",
        ),
        (
            "pre-tool-rewrite",
            _calls(PreToolUse=1),
            _stream("", tool_results=[live_probe.ORIGINAL]),
            "unsupported",
        ),
        ("stop-block", _calls(Stop=1), _stream("READY"), "unsupported"),
        ("subagent-stop-block", _calls(SubagentStop=1), _stream("ok"), "unsupported"),
        (
            "subagent-start-context",
            _calls(SubagentStart=1),
            _stream(f"{live_probe.CTX}SUBAGENT_START"),
            "supported",
        ),
        (
            "pre-tool-ask",
            _calls(PreToolUse=1, PermissionRequest=1),
            _stream("refused"),
            "supported",
        ),
        ("pre-tool-ask", _calls(PreToolUse=1, PostToolUse=1), _stream("ran"), "unsupported"),
        ("pre-tool-ask", _calls(PreToolUse=1), _stream("refused"), "supported"),
        ("pre-tool-ask", _calls(), _stream("refused"), "inconclusive"),
        ("session-end-observe", _calls(), _stream("READY"), "inconclusive"),
        ("session-end-observe", _calls(SessionEnd=1), _stream("READY"), "supported"),
        (
            "agent-rewrite",
            _calls(PreToolUse=1, SubagentStart=1),
            _stream("", models=["claude-haiku-4-5", "claude-sonnet-5-5"]),
            "supported",
        ),
        (
            "agent-rewrite",
            _calls(PreToolUse=1, SubagentStart=1),
            _stream("", models=["claude-haiku-4-5"]),
            "unsupported",
        ),
        (
            "agent-rewrite",
            _calls(PreToolUse=1),
            _stream("", models=["claude-haiku-4-5"]),
            "inconclusive",
        ),
        ("pre-compact-block", _calls(PreCompact=1), _stream("", error=True), "supported"),
        ("pre-compact-block", _calls(PreCompact=1), _stream("", compacted=True), "unsupported"),
    ],
)
def test_evaluate_separates_supported_unsupported_and_inconclusive(name, records, stream, state):
    assert live_probe.evaluate(_case(name), _evidence(records, stream))[0] == state


def test_stop_block_is_supported_only_after_a_second_stop_acts_on_the_reason():
    records = [
        {"event": "Stop", "input": {"stop_hook_active": False}},
        {"event": "Stop", "input": {"stop_hook_active": True}},
    ]

    evidence = _evidence(records, _stream(live_probe.STOP_ACK))

    assert live_probe.evaluate(_case("stop-block"), evidence) == ("supported", [])


def test_timeout_is_inconclusive_even_with_every_callback_present():
    evidence = _evidence(_calls(PreToolUse=1), _stream(f"{live_probe.CTX}PRE_TOOL"), timed_out=True)

    state, reasons = live_probe.evaluate(_case("pre-tool-context"), evidence)

    assert state == "inconclusive"
    assert any("timed out" in reason for reason in reasons)


def test_claude_record_keeps_shapes_and_counts_without_hook_content():
    records = [
        {
            "event": "PreToolUse",
            "input": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "secret"},
        }
    ]
    case = _case("pre-tool-deny")
    evidence = _evidence(records, _stream("refused", tool_results=[live_probe.DENY_REASON]))

    record = live_probe.claude_record(
        case,
        evidence,
        verdict=live_probe.evaluate(case, evidence),
        version="2.1.287",
        started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T00:01:00Z",
        removed={"scratch": True},
    )

    assert "secret" not in json.dumps(record)
    assert record["format_version"] == "watchdog.probe-result.v1"
    assert record["classification"] == "live_provider"
    callbacks = record["stages"]["callbacks"]
    assert (callbacks["expected"], callbacks["observed"], callbacks["missing"]) == (1, 1, 0)
    capability = record["capabilities"]["pre-tool-deny"]
    assert capability["state"] == "supported"
    assert capability["callbacks"]["PreToolUse"]["tool_names"] == ["Bash"]
    assert record["cleanup"]["complete"] is True


def test_incomplete_cleanup_is_reported_not_assumed():
    case = _case("stop-block")
    evidence = _evidence([], [])

    record = live_probe.claude_record(
        case,
        evidence,
        verdict=live_probe.evaluate(case, evidence),
        version=None,
        started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T00:01:00Z",
        removed={"scratch": True, "provider_state": False},
    )

    assert record["cleanup"]["complete"] is False
    assert record["stages"]["provider_dispatch"]["state"] == "failed"
    assert record["result"]["state"] == "incomplete"


def test_parse_stream_skips_lines_that_are_not_json_objects():
    text = '{"type": "result"}\nnoise\n[1]\n'

    assert live_probe.parse_stream(text) == [{"type": "result"}]


def test_claude_probe_refuses_to_nest_inside_a_claude_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")

    with pytest.raises(RuntimeError, match="standalone"):
        live_probe.require_standalone_claude()


def test_claude_helpers_never_launch_a_subprocess(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("offline helper launched a subprocess"))
    monkeypatch.setattr(live_probe.subprocess, "run", forbidden)
    monkeypatch.setattr(live_probe.subprocess, "Popen", forbidden)

    for case in live_probe.CLAUDE_CASES.values():
        live_probe.claude_command("claude", case, Path("s.json"), 0.5)
        live_probe.claude_stdin(case)
        live_probe.evaluate(case, _evidence([], []))

    forbidden.assert_not_called()


def _hook(monkeypatch, capsys, directory, payload):
    monkeypatch.setattr(
        live_probe.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )
    assert live_probe.handle_routed(directory) == 0
    return json.loads(capsys.readouterr().out)


def test_routed_handler_follows_the_case_named_in_the_prompt(monkeypatch, capsys, tmp_path):
    (tmp_path / ".wd139").mkdir()
    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "WD139[pre-tool-deny] go"}

    assert _hook(monkeypatch, capsys, tmp_path, prompt) == {}
    denied = _hook(monkeypatch, capsys, tmp_path, {"hook_event_name": "PreToolUse"})

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    logged = [
        json.loads(line)
        for line in (tmp_path / ".wd139" / "probe.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["case"] for record in logged] == ["pre-tool-deny", "pre-tool-deny"]


def test_routed_handler_disarms_after_a_stop_so_the_next_session_start_is_quiet(
    monkeypatch, capsys, tmp_path
):
    (tmp_path / ".wd139").mkdir()
    (tmp_path / ".wd139" / "case").write_text(live_probe.FIRST_CASE, encoding="utf-8")
    start = {"hook_event_name": "SessionStart"}

    armed = _hook(monkeypatch, capsys, tmp_path, start)
    _hook(monkeypatch, capsys, tmp_path, {"hook_event_name": "Stop"})
    quiet = _hook(monkeypatch, capsys, tmp_path, start)

    assert armed["hookSpecificOutput"]["additionalContext"] == f"{live_probe.CTX}SESSION_START"
    assert quiet == {}


def test_routed_handler_keeps_a_stop_block_armed_until_the_second_stop(
    monkeypatch, capsys, tmp_path
):
    (tmp_path / ".wd139").mkdir()
    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "WD139[stop-block] go"}
    _hook(monkeypatch, capsys, tmp_path, prompt)

    first = _hook(monkeypatch, capsys, tmp_path, {"hook_event_name": "Stop"})
    second = _hook(
        monkeypatch, capsys, tmp_path, {"hook_event_name": "Stop", "stop_hook_active": True}
    )

    assert first["decision"] == "block"
    assert second == {}


def test_routed_handler_snapshots_files_per_case_and_clears_the_sentinel(
    monkeypatch, capsys, tmp_path
):
    (tmp_path / ".wd139").mkdir()
    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "WD139[permission-allow] go"}
    _hook(monkeypatch, capsys, tmp_path, prompt)
    (tmp_path / live_probe.SENTINEL).write_text("x", encoding="utf-8")

    _hook(monkeypatch, capsys, tmp_path, {"hook_event_name": "Stop"})
    next_prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "WD139[permission-deny] go"}
    _hook(monkeypatch, capsys, tmp_path, next_prompt)

    logged = [
        json.loads(line)
        for line in (tmp_path / ".wd139" / "probe.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["files"] for record in logged if "files" in record] == [[live_probe.SENTINEL]]
    assert not (tmp_path / live_probe.SENTINEL).exists()


def test_desktop_kit_covers_single_prompt_cases_and_routes_by_prefix():
    cases = {case.name: case for case in live_probe.desktop_cases()}

    assert "pre-compact-block" not in cases and "agent-rewrite" not in cases
    assert {"pre-tool-ask", "permission-allow", "agent-deny", "stop-block"} <= set(cases)
    prompt = live_probe.desktop_prompt(cases["permission-allow"])
    assert prompt.startswith("WD139[permission-allow] ")
    assert "printf WD139_PERMISSION" in prompt and "echo WD139_PERMISSION" not in prompt


def test_desktop_settings_route_every_event_through_one_handler(tmp_path):
    settings = live_probe.desktop_settings(tmp_path)

    assert set(settings["hooks"]) == {event for event, _ in live_probe.DESKTOP_EVENTS}
    commands = {entry[0]["hooks"][0]["command"] for entry in settings["hooks"].values()}
    assert len(commands) == 1 and "claude-routed-handler" in commands.pop()
    assert settings["permissions"]["deny"] == ["PowerShell"]
    json.dumps(settings)


def test_prepare_refuses_a_folder_inside_a_registered_project(tmp_path):
    root = tmp_path / "registered"
    root.mkdir()

    with pytest.raises(ValueError, match="inside a registered"):
        live_probe.prepare_desktop(root / "scratch", [root])


def test_collect_ignores_a_later_chats_session_start_tagged_with_the_same_case(tmp_path):
    control = tmp_path / ".wd139"
    control.mkdir()
    entries = [
        {"event": "UserPromptSubmit", "case": "pre-tool-ask", "input": {"session_id": "a"}},
        {"event": "PreToolUse", "case": "pre-tool-ask", "input": {"session_id": "a"}},
        # The denied dialog ended chat "a" without a Stop, so the case stayed armed.
        {"event": "SessionStart", "case": "pre-tool-ask", "input": {"session_id": "b"}},
    ]
    (control / "probe.ndjson").write_text(
        "\n".join(json.dumps(entry) for entry in entries), encoding="utf-8"
    )

    (record,) = live_probe.collect_desktop(tmp_path, clean=False)["records"]

    callbacks = record["capabilities"]["pre-tool-ask"]["callbacks"]
    assert callbacks["PreToolUse"]["observed"] == 1


def test_collect_judges_a_desktop_case_from_the_log_and_transcript(tmp_path):
    control = tmp_path / ".wd139"
    control.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "user", "version": "2.1.300", "message": {"content": "go"}},
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": live_probe.DENY_REASON}]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-haiku-4-5",
                        "content": [{"type": "text", "text": "it was refused"}],
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    payload = {"session_id": "s1", "transcript_path": str(transcript)}
    entries = [
        {"event": "UserPromptSubmit", "case": "pre-tool-deny", "input": payload},
        {"event": "PreToolUse", "case": "pre-tool-deny", "input": payload},
    ]
    (control / "probe.ndjson").write_text(
        "\n".join(json.dumps(entry) for entry in entries), encoding="utf-8"
    )

    result = live_probe.collect_desktop(tmp_path, clean=False)

    (record,) = result["records"]
    assert record["capabilities"]["pre-tool-deny"]["state"] == "supported"
    assert record["provenance"]["provider"] == {
        "name": "claude",
        "version": "2.1.300",
        "surface": "desktop",
    }
    assert record["cleanup"]["complete"] is False
    assert "pre-tool-deny" not in result["not_run"] and "stop-block" in result["not_run"]
