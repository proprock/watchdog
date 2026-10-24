"""Offline contracts for operator-driven WD-153 desktop probes."""

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
try:
    probe = importlib.import_module("codex_desktop_probe")
finally:
    sys.path.pop(0)


def prepare(tmp_path, case="session-start-context"):
    return probe.prepare(tmp_path / "scratch", case, [])


def transcript(root, session="session-1", *, reply=None, complete=True, version="0.999.0"):
    manifest = probe.load_manifest(root)
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": session,
                "cwd": str(root),
                "cli_version": version,
                "source": "vscode",
                "originator": "Codex Desktop",
            },
        }
    ]
    rows.append(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": reply or "NONE"}],
            },
        }
    )
    if complete:
        rows.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    path = root / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": session,
        "cwd": str(root),
        "transcript_path": str(path),
    }
    probe.respond(root, payload)
    return manifest, path


def test_prepare_is_one_case_and_does_not_reveal_hook_context_in_prompt(tmp_path):
    kit = prepare(tmp_path)
    root = Path(kit["dir"])
    hooks = json.loads((root / ".codex/hooks.json").read_text())
    assert probe.probes.CODEX_CONTEXT_MARKER not in kit["prompt"]
    assert "SessionStart" in hooks["hooks"]
    assert "--root" in hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert not (root / ".codex/config.toml").exists()
    assert hooks["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 30
    assert hooks["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3


def test_prepare_refuses_existing_content_and_registered_roots(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="empty|exist"):
        probe.prepare(root, "session-start-context", [])
    with pytest.raises(ValueError, match="registered"):
        probe.prepare(tmp_path / "registered/child", "session-start-context", [tmp_path])
    assert (root / "keep.txt").read_text() == "keep"


def test_handler_fails_open_outside_scratch_and_on_wrong_event(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    assert probe.respond(root, {"hook_event_name": "SessionStart", "cwd": str(tmp_path)}) == {}
    assert probe.respond(root, {"hook_event_name": "PreToolUse", "cwd": str(root)}) == {}
    assert not (root / ".wd153/probe.ndjson").exists()


@pytest.mark.parametrize("complete,version", [(False, "0.999.0"), (True, None)])
def test_callback_without_completion_or_version_is_inconclusive(tmp_path, complete, version):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, complete=complete, version=version, reply=probe.probes.CODEX_CONTEXT_MARKER)
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_same_session_completed_context_is_sanitized_and_desktop_scoped(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "supported"
    assert record["provenance"]["provider"] == {
        "name": "codex",
        "version": "0.999.0",
        "surface": "desktop",
    }
    assert record["provenance"]["harness"]["version"] == "1.2.3"
    assert str(root) not in json.dumps(record)
    assert not (root / ".codex/hooks.json").exists()
    assert (root / "rollout.jsonl").exists()
    assert record["cleanup"]["complete"] is False


def test_prompt_echo_and_cross_session_rollout_never_establish_context(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    _, path = transcript(root)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["payload"]["role"] = "user"
    rows[1]["payload"]["content"][0]["text"] = probe.probes.CODEX_CONTEXT_MARKER
    path.write_text("\n".join(map(json.dumps, rows)))
    assert (
        probe.collect(root, desktop_version="1.2.3")["capabilities"]["session-start-context"][
            "state"
        ]
        != "supported"
    )


def test_mismatched_session_is_inconclusive(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    path.write_text(path.read_text().replace('"session-1"', '"other-session"'))
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_changed_hook_is_preserved_and_reported(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    hook = root / ".codex/hooks.json"
    hook.write_text('{"changed": true}')
    record = probe.collect(root, desktop_version="1.2.3")
    assert hook.exists()
    assert record["cleanup"]["complete"] is False
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_wrong_tool_has_no_response_or_log_and_stop_guard_is_fail_open(tmp_path):
    root = Path(prepare(tmp_path, "pre-tool-bash-deny")["dir"])
    assert (
        probe.respond(
            root, {"cwd": str(root), "hook_event_name": "PreToolUse", "tool_name": "apply_patch"}
        )
        == {}
    )
    assert not (root / ".wd153/probe.ndjson").exists()
    other = tmp_path / "other"
    probe.prepare(other, "stop-block", [])
    assert (
        probe.respond(
            other, {"cwd": str(other), "hook_event_name": "Stop", "stop_hook_active": True}
        )
        == {}
    )


def test_two_sessions_cannot_be_combined_into_positive_evidence(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    probe.respond(root, {"cwd": str(root), "hook_event_name": "Stop", "session_id": "other"})
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_direct_provider_evidence_does_not_claim_store_delivery_or_completed_cleanup(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["stages"]["end_to_end_delivery"]["state"] == "not_applicable"
    assert record["stages"]["end_to_end_delivery"]["persisted"] == 0
    assert record["result"]["state"] == "incomplete"
    assert len(record["cleanup"]["resources"]) == 3


def test_prompt_context_and_session_end_have_their_own_cases(tmp_path):
    root = Path(prepare(tmp_path, "user-prompt-context")["dir"])
    response = probe.respond(root, {"cwd": str(root), "hook_event_name": "UserPromptSubmit"})
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "session-end-observe" in probe.CASES


def test_context_token_read_from_a_tool_is_not_hook_context_evidence(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    with path.open("a") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": "{}",
                    },
                }
            )
        )
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_retries_in_same_session_are_not_merged(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    payload = {"cwd": str(root), "hook_event_name": "UserPromptSubmit", "session_id": "session-1"}
    probe.respond(root, payload)
    probe.respond(root, payload)
    assert (
        probe.collect(root, desktop_version="1.2.3")["capabilities"]["session-start-context"][
            "state"
        ]
        == "inconclusive"
    )


def test_explicit_desktop_transcript_without_callback_records_dispatch_only(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    _, path = transcript(root)
    (root / ".wd153/probe.ndjson").unlink()
    record = probe.collect(root, desktop_version="1.2.3", manual_transcript=path)
    assert record["stages"]["provider_dispatch"]["state"] == "confirmed"
    assert record["stages"]["provider_dispatch"]["attempts"] == 1
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def test_cli_transcript_cannot_establish_desktop_capability(tmp_path):
    root = Path(prepare(tmp_path)["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    path.write_text(path.read_text().replace('"vscode"', '"cli"'))
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == "inconclusive"


def subagent_transcripts(tmp_path, *, child_reply=None, child_task=None):
    root = Path(prepare(tmp_path, "subagent-start-context")["dir"])
    manifest, parent = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    rows = probe.probes.parse_stream(parent.read_text())
    rows.insert(
        1,
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": json.dumps({"message": child_task or probe.SUBAGENT_TASK}),
            },
        },
    )
    parent.write_text("\n".join(map(json.dumps, rows)))
    child = root / "child.jsonl"
    child_rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child-1",
                "cwd": str(root),
                "cli_version": "0.999.0",
                "originator": "Codex Desktop",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "session-1"}}},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"text": child_task or probe.SUBAGENT_TASK}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": child_reply or probe.probes.CODEX_CONTEXT_MARKER}],
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    child.write_text("\n".join(map(json.dumps, child_rows)))
    probe.respond(
        root,
        {
            "cwd": str(root),
            "hook_event_name": "SubagentStart",
            "session_id": "session-1",
            "agent_id": "child-1",
            "transcript_path": str(child),
        },
    )
    return root, manifest, parent, child


def test_subagent_context_uses_verified_child_assistant_output(tmp_path):
    root, _, _, _ = subagent_transcripts(tmp_path)
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["subagent-start-context"]["state"] == "supported"


def test_subagent_context_reads_exact_automatically_archived_child(tmp_path):
    root, _, _, child = subagent_transcripts(tmp_path)
    original = tmp_path / "provider/sessions/2026/10/24" / child.name
    original.parent.mkdir(parents=True)
    archived = tmp_path / "provider/archived_sessions" / child.name
    archived.parent.mkdir()
    child.rename(archived)
    log = root / ".wd153/probe.ndjson"
    records = probe.probes._read_records(log)
    records[-1]["input"]["transcript_path"] = str(original)
    log.write_text("\n".join(map(json.dumps, records)))
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["subagent-start-context"]["state"] == "supported"


@pytest.mark.parametrize(
    "reply,expected", [("NONE", "unsupported"), ("TOKEN-arbitrary", "inconclusive")]
)
def test_parent_token_does_not_substitute_for_child_output(tmp_path, reply, expected):
    root, _, _, _ = subagent_transcripts(tmp_path, child_reply=reply)
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["subagent-start-context"]["state"] == expected


@pytest.mark.parametrize("altered", [False, True])
def test_recollection_requires_unchanged_owned_disabled_hook(tmp_path, altered):
    root = Path(prepare(tmp_path)["dir"])
    transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    probe.collect(root, desktop_version="1.2.3")
    if altered:
        (root / ".codex/hooks.json.wd153-disabled").write_text("{}")
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["session-start-context"]["state"] == (
        "inconclusive" if altered else "supported"
    )


@pytest.mark.parametrize(
    "defect",
    ["missing", "identity", "parent", "version", "input", "tool", "workload", "two-children"],
)
def test_subagent_context_rejects_unverified_or_contaminated_child(tmp_path, defect):
    root, _, parent, child = subagent_transcripts(tmp_path)
    rows = probe.probes.parse_stream(child.read_text())
    if defect == "missing":
        child.unlink()
    elif defect == "identity":
        rows[0]["payload"]["id"] = "other-child"
    elif defect == "parent":
        rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "other"
    elif defect == "version":
        rows[0]["payload"]["cli_version"] = "0.888.0"
    elif defect == "input":
        rows[2]["payload"]["role"] = "user"
    elif defect == "tool":
        rows.insert(
            2, {"type": "response_item", "payload": {"type": "function_call", "name": "read_file"}}
        )
    elif defect == "workload":
        parent.write_text(parent.read_text().replace(probe.SUBAGENT_TASK, "Reply TOKEN-arbitrary"))
    else:
        probe.respond(
            root,
            {
                "cwd": str(root),
                "hook_event_name": "SubagentStart",
                "session_id": "session-1",
                "agent_id": "child-2",
                "transcript_path": str(child),
            },
        )
    if defect != "missing":
        child.write_text("\n".join(map(json.dumps, rows)))
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["subagent-start-context"]["state"] == "inconclusive"


@pytest.mark.parametrize(
    "defect", [None, "identity", "filename", "exists", "cwd", "stale", "origin", "version"]
)
def test_explicit_archived_transcript_requires_same_relocated_session(tmp_path, defect):
    root = Path(prepare(tmp_path, "session-end-observe")["dir"])
    _, original = transcript(root)
    sessions = tmp_path / "provider/sessions/2026/10/24"
    sessions.mkdir(parents=True)
    original = original.rename(sessions / original.name)
    log = root / ".wd153/probe.ndjson"
    records = probe.probes._read_records(log)
    records[0]["input"]["transcript_path"] = str(original)
    log.write_text("\n".join(map(json.dumps, records)))
    archived = tmp_path / "provider/archived_sessions" / original.name
    archived.parent.mkdir()
    if defect == "exists":
        shutil.copyfile(original, archived)
    else:
        original.rename(archived)
    if defect == "filename":
        archived = archived.rename(archived.with_name("unrelated.jsonl"))
    replacements = {
        "identity": ('"session-1"', '"other"'),
        "cwd": (str(root).replace("\\", "\\\\"), "unrelated"),
        "origin": ('"vscode"', '"cli"'),
        "version": ('"0.999.0"', "null"),
    }
    if defect in replacements:
        archived.write_text(archived.read_text().replace(*replacements[defect]))
    if defect == "stale":
        os.utime(archived, (1, 1))
    record = probe.collect(root, desktop_version="1.2.3", manual_transcript=archived)
    assert record["stages"]["provider_dispatch"]["state"] == (
        "confirmed" if defect is None else "unknown"
    )
    assert record["capabilities"]["session-end-observe"]["state"] == "inconclusive"


@pytest.mark.parametrize(
    "case,callback,expected",
    [
        ("interrupt-observe", True, "supported"),
        ("interrupt-observe", False, "inconclusive"),
        ("session-start-context", True, "inconclusive"),
    ],
)
def test_aborted_turn_only_establishes_interrupt_observation(tmp_path, case, callback, expected):
    root = Path(prepare(tmp_path, case)["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    path.write_text(path.read_text().replace("task_complete", "task_aborted"))
    if callback:
        probe.respond(
            root,
            {
                "cwd": str(root),
                "session_id": "session-1",
                "hook_event_name": "Interrupt",
                "transcript_path": str(path),
            },
        )
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"][case]["state"] == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows command override")
def test_generated_windows_command_accepts_stdin_and_writes_callback(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    root = Path(prepare(tmp_path)["dir"])
    hooks = json.loads((root / ".codex/hooks.json").read_text())
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", handler["commandWindows"]],
        input=json.dumps({"cwd": str(root), "hook_event_name": "SessionStart"}),
        text=True,
        capture_output=True,
        cwd=root,
        timeout=handler["timeout"],
        check=True,
        creationflags=probe.probes.hidden_creationflags(),
    )
    assert probe.probes.CODEX_CONTEXT_MARKER in result.stdout
    assert len(probe.probes._read_records(root / ".wd153/probe.ndjson")) == 1


@pytest.mark.parametrize(
    "reply,expected", [("NONE", "unsupported"), ("WD139C_CONTEXT", "supported")]
)
def test_desktop_compact_excludes_only_unrelated_session_end(tmp_path, reply, expected):
    root = Path(prepare(tmp_path, "pre-compact-context")["dir"])
    _, path = transcript(root, reply=reply)
    rows = probe.probes.parse_stream(path.read_text())
    rows.insert(1, {"type": "compacted", "payload": {"message": "synthetic compaction"}})
    path.write_text("\n".join(map(json.dumps, rows)))
    probe.respond(
        root,
        {
            "cwd": str(root),
            "hook_event_name": "PreCompact",
            "session_id": "session-1",
            "transcript_path": str(path),
        },
    )
    probe.respond(
        root,
        {
            "cwd": str(root),
            "hook_event_name": "SessionEnd",
            "session_id": "unrelated",
            "transcript_path": str(root / "unrelated.jsonl"),
        },
    )
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["pre-compact-context"]["state"] == expected
    assert (
        "The non-interactive runner may not expose manual compaction."
        not in record["result"]["limitations"]
    )


@pytest.mark.parametrize(
    "conflict",
    [
        "PreCompact",
        "UserPromptSubmit",
        "Stop",
        "SessionStart",
        "missing-primary",
        "missing-end-session",
        "session-end-case",
    ],
)
def test_unrelated_session_end_does_not_relax_other_session_conflicts(tmp_path, conflict):
    case = "session-end-observe" if conflict == "session-end-case" else "pre-compact-context"
    root = Path(prepare(tmp_path, case)["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    if case == "session-end-observe":
        probe.respond(
            root,
            {
                "cwd": str(root),
                "hook_event_name": "SessionEnd",
                "session_id": "session-1",
                "transcript_path": str(path),
            },
        )
    elif conflict != "missing-primary":
        probe.respond(
            root,
            {
                "cwd": str(root),
                "hook_event_name": "PreCompact",
                "session_id": "other" if conflict == "PreCompact" else "session-1",
                "transcript_path": str(path),
            },
        )
    if conflict in {"UserPromptSubmit", "Stop", "SessionStart"}:
        probe.respond(
            root,
            {
                "cwd": str(root),
                "hook_event_name": conflict,
                "session_id": "other",
                "transcript_path": str(path),
            },
        )
    probe.respond(
        root,
        {
            "cwd": str(root),
            "hook_event_name": "SessionEnd",
            "session_id": None if conflict == "missing-end-session" else "unrelated",
            "transcript_path": str(root / "unrelated.jsonl"),
        },
    )
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"][case]["state"] == "inconclusive"


@pytest.mark.parametrize(
    "defect",
    ["no-boundary", "late-boundary", "no-answer", "before-only", "tools", "early-completion"],
)
def test_desktop_compact_requires_completed_assistant_answer_after_boundary(tmp_path, defect):
    root = Path(prepare(tmp_path, "pre-compact-context")["dir"])
    _, path = transcript(root, reply=probe.probes.CODEX_CONTEXT_MARKER)
    rows = probe.probes.parse_stream(path.read_text())
    boundary = {"type": "compacted", "payload": {"message": "synthetic compaction"}}
    if defect == "late-boundary":
        rows.append(boundary)
    elif defect != "no-boundary":
        rows.insert(1, boundary)
    if defect == "no-answer":
        rows[2]["payload"]["role"] = "user"
    elif defect == "before-only":
        rows.insert(1, rows[2])
        rows[3] = {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"text": "NONE"}]},
        }
    elif defect == "tools":
        rows.insert(
            2, {"type": "response_item", "payload": {"type": "function_call", "name": "read_file"}}
        )
    elif defect == "early-completion":
        rows[2], rows[3] = rows[3], rows[2]
    path.write_text("\n".join(map(json.dumps, rows)))
    probe.respond(
        root,
        {
            "cwd": str(root),
            "hook_event_name": "PreCompact",
            "session_id": "session-1",
            "transcript_path": str(path),
        },
    )
    record = probe.collect(root, desktop_version="1.2.3")
    assert record["capabilities"]["pre-compact-context"]["state"] == (
        "unsupported" if defect == "before-only" else "inconclusive"
    )
