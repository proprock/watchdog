"""WD-153 operator-driven Codex desktop hooks, one scratch repository per case.

Preparing and collecting never launch a provider or change user-level configuration.
The operator reviews native hook trust and drives the desktop session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import live_probe as probes

CASES = {
    name: case
    for name, case in probes.CODEX_CASES.items()
    if not case.code_mode and "mcp" not in name and "local" not in name
}
CASES["session-end-observe"] = probes.CodexCase(
    "session-end-observe",
    (("SessionEnd", None),),
    {"SessionEnd": 1},
    {},
    "{} (observation only)",
    ("Reply READY, then wait for the operator to close this chat.",),
    probes._codex_observe_verdict("SessionEnd"),
)
CASES["user-prompt-context"] = probes.CodexCase(
    "user-prompt-context",
    (("UserPromptSubmit", None),),
    {"UserPromptSubmit": 1},
    {"UserPromptSubmit": probes._codex_context("UserPromptSubmit", probes.CODEX_CONTEXT_MARKER)},
    "hookSpecificOutput.additionalContext",
    ("Repeat the hook token, or NONE if absent.",),
    probes._codex_context_verdict(probes.CODEX_CONTEXT_MARKER, "UserPromptSubmit"),
)
CONTROL = ".wd153"
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?")
SUBAGENT_TASK = (
    "Return only the exact token supplied to you by the SubagentStart hook, "
    "or NONE if the hook supplied no token. Do not use tools, invent or guess a token."
)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain_path(path: Path) -> Path:
    """Reject links before resolving so owned files cannot escape through junctions."""
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("scratch paths must not contain symlinks or junctions")
    return path.resolve()


def load_manifest(root: Path) -> dict:
    plain_path(root)
    return json.loads((root / CONTROL / "manifest.json").read_text(encoding="utf-8"))


def prepare(root: Path, case_name: str, roots: list[Path]) -> dict:
    root = plain_path(root)
    probes.ensure_scratch_outside_roots(root, [*roots, Path(__file__).resolve().parents[1]])
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("scratch directory must be new or empty")
    case = CASES[case_name]
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(root)],
        check=True,
        capture_output=True,
        creationflags=probes.hidden_creationflags(),
    )
    (root / CONTROL).mkdir()
    (root / ".codex").mkdir()
    argv = [sys.executable, str(Path(__file__).resolve()), "handler", "--root", str(root)]
    handler = {
        "type": "command",
        "command": shlex.join(argv),
        "timeout": 3,
        "additionalContextLimit": 128,
    }
    if os.name == "nt":
        handler["commandWindows"] = "& " + " ".join(
            "'" + value.replace("'", "''") + "'" for value in argv
        )
    events = dict(case.events)
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        events.setdefault(event, None)
    hooks = {}
    for event, matcher in events.items():
        event_handler = {**handler, "timeout": 3 if event in {"SessionEnd", "Interrupt"} else 30}
        entry = {"hooks": [event_handler]}
        if matcher is not None:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    hook = root / ".codex/hooks.json"
    hook.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "case": case_name,
        "run_id": uuid4().hex,
        "started_at": now(),
        "hook_sha256": digest(hook),
    }
    (root / CONTROL / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    prompt = case.prompts[0].replace("Bash command", "PowerShell shell command")
    prompt = prompt.replace(probes.CODEX_CONTEXT_MARKER, "the exact token supplied by the hook")
    if case_name.startswith("permission-bash-"):
        prompt += " Explicitly request sandbox_permissions=require_escalated for this scratch test."
    if case_name.startswith(("pre-compact", "post-compact")):
        prompt = "Reply READY. Then wait for the operator to use the app's manual compaction."
    if case_name == "subagent-start-context":
        prompt = (
            "Start exactly one general-purpose subagent with this exact task: "
            + SUBAGENT_TASK
            + " The parent must not supply, invent or suggest a token to the child. "
            "Wait for that one child and relay its exact reply. Do not start another child."
        )
    steps = [
        "Open this scratch folder in Codex desktop. Record the app version from About.",
        "Review and trust only this project's exact hook definition using the native hook UI.",
        "Start a fresh chat in this folder after trust. Send the supplied prompt once.",
        "Wait for the turn to finish. Do not retry in the same scratch folder.",
        "Return here for collection. If trust or the capability is unavailable, report that fact.",
    ]
    if "compact" in case_name:
        steps.insert(3, "Manually compact, then ask: Repeat the hook token, or NONE if absent.")
    if case_name == "interrupt-observe":
        steps.insert(3, "Interrupt the harmless sleep using the app Stop control.")
    return {"dir": str(root), "case": case_name, "prompt": prompt, "steps": steps}


def respond(root: Path, payload: dict) -> dict:
    """Return only the selected probe response; unrelated callbacks are fail-open."""
    manifest = load_manifest(root)
    case = CASES[manifest["case"]]
    event = payload.get("hook_event_name")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or Path(cwd).resolve() != root.resolve():
        return {}
    allowed = dict(case.events)
    observers = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    if event not in allowed and event not in observers:
        return {}
    matcher = allowed.get(event)
    if matcher and event in {"PreToolUse", "PostToolUse", "PermissionRequest"}:
        tool = payload.get("tool_name")
        if not isinstance(tool, str) or not re.search(matcher, tool):
            return {}
    response = probes.codex_hook_response(case, event, payload)
    probes._append_log(
        root / CONTROL / "probe.ndjson",
        {
            "run_id": manifest["run_id"],
            "at": now(),
            "event": event,
            "input": payload,
            "output": response,
        },
    )
    return response


def archived_relocation(original: Path, replacement: Path) -> bool:
    """Accept only an explicit move from sessions to its sibling archive directory."""
    original, replacement = plain_path(original), plain_path(replacement)
    return (
        not original.exists()
        and replacement.name == original.name
        and replacement.parent.name == "archived_sessions"
        and any(
            parent.name == "sessions" and parent.parent == replacement.parent.parent
            for parent in original.parents
        )
    )


def child_context(
    path: Path,
    agent_id: str,
    parent_id: str,
    root: Path,
    version: str,
    started_at: str,
    parent_rows: list[dict],
) -> tuple[str, list[str]]:
    """Read only the callback-named child, with isolated task and output provenance."""
    if not path.exists():
        for parent in path.parents:
            if parent.name == "sessions":
                archived = parent.parent / "archived_sessions" / path.name
                if archived_relocation(path, archived):
                    path = archived
                break
    raw = probes._read_rollout(path)
    rows = probes.parse_stream(raw)
    metadata = [row.get("payload") for row in rows if row.get("type") == "session_meta"]
    if len(metadata) != 1 or not isinstance(meta := metadata[0], dict):
        return "", ["One readable child transcript is required."]
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if (
        meta.get("id") != agent_id
        or Path(meta.get("cwd", "")).resolve() != root.resolve()
        or meta.get("cli_version") != version
        or meta.get("originator") != "Codex Desktop"
        or not isinstance(spawn, dict)
        or spawn.get("parent_thread_id") != parent_id
        or path.stat().st_mtime < datetime.fromisoformat(started_at).timestamp()
    ):
        return "", ["Child transcript identity, parent, version or freshness is unverified."]
    tasks = []
    for row in parent_rows:
        payload = row.get("payload")
        if (
            row.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "custom_tool_call"
        ):
            return "", ["Parent used another tool; hook-token source is not isolated."]
        if (
            row.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "function_call"
        ):
            tool = str(payload.get("name", "")).split(".")[-1]
            if tool in {"wait", "wait_agent"}:
                continue
            if tool != "spawn_agent":
                return "", ["Parent used another tool; hook-token source is not isolated."]
            try:
                arguments = json.loads(payload.get("arguments", ""))
            except (TypeError, ValueError):
                arguments = None
            tasks.append(arguments.get("message") if isinstance(arguments, dict) else None)
    if tasks != [SUBAGENT_TASK]:
        return "", ["Exactly one child with the isolated hook-token task is required."]
    if any(
        row.get("type") == "response_item"
        and isinstance(payload := row.get("payload"), dict)
        and payload.get("type") in {"function_call", "custom_tool_call"}
        for row in rows
    ):
        return "", ["Child used tools; hook-token source is not isolated."]
    text = observed_text(raw, assistant_only=True).strip()
    if text not in {probes.CODEX_CONTEXT_MARKER, "NONE"} or not any(
        row.get("type") == "event_msg"
        and isinstance(payload := row.get("payload"), dict)
        and payload.get("type") == "task_complete"
        for row in rows
    ):
        return "", ["Child did not complete with only the hook token or NONE."]
    return text, []


def read_evidence(
    root: Path, manifest: dict, manual_transcript: Path | None = None
) -> tuple[probes.CodexEvidence, str | None, list[str]]:
    records = probes._read_records(root / CONTROL / "probe.ndjson")
    records = [row for row in records if row.get("run_id") == manifest["run_id"]]
    case = CASES[manifest["case"]]
    if "SessionEnd" not in case.expected:
        primary = [row for row in records if row.get("event") != "SessionEnd"]
        primary_sessions = {
            payload.get("session_id")
            for row in primary
            if isinstance(payload := row.get("input"), dict)
        }
        if (
            len(primary_sessions) == 1
            and isinstance(session := next(iter(primary_sessions)), str)
            and session
            and all(
                isinstance(payload := row.get("input"), dict)
                and payload.get("session_id") == session
                for row in primary
            )
            and all(
                sum(row.get("event") == event for row in primary) >= minimum
                for event, minimum in case.expected.items()
            )
        ):
            # A late unrelated chat close is not evidence for this owned workload.
            records = [
                row
                for row in records
                if not (
                    row.get("event") == "SessionEnd"
                    and isinstance(payload := row.get("input"), dict)
                    and payload.get("hook_event_name") == "SessionEnd"
                    and isinstance(other := payload.get("session_id"), str)
                    and other
                    and other != session
                )
            ]
    problems: list[str] = []
    if (
        "compact" not in manifest["case"]
        and sum(row.get("event") == "UserPromptSubmit" for row in records) > 1
    ):
        problems.append(
            "Multiple prompts were submitted; use a fresh single-attempt scratch folder."
        )
    inputs = [payload for row in records if isinstance(payload := row.get("input"), dict)]
    sessions = {payload.get("session_id") for payload in inputs}
    if (inputs or manual_transcript is None) and (
        len(sessions) != 1 or not all(isinstance(s, str) and s for s in sessions)
    ):
        problems.append("Exactly one callback session is required; use a fresh scratch folder.")
    child_inputs = [
        payload
        for payload in inputs
        if manifest["case"] == "subagent-start-context"
        and payload.get("hook_event_name") == "SubagentStart"
    ]
    paths = {
        path
        for payload in inputs
        if payload not in child_inputs
        if isinstance(path := payload.get("transcript_path"), str) and path
    }
    if manual_transcript is not None:
        if (
            paths
            and paths != {str(manual_transcript)}
            and not (
                len(paths) == 1 and archived_relocation(Path(next(iter(paths))), manual_transcript)
            )
        ):
            problems.append("Explicit transcript differs from the callback transcript.")
        paths = {str(manual_transcript)}
    raw = ""
    version = None
    completed = False
    if len(paths) == 1 and not problems:
        path = Path(next(iter(paths)))
        raw = probes._read_rollout(path)
        rows = probes.parse_stream(raw)
        metadata = [
            payload
            for row in rows
            if row.get("type") == "session_meta" and isinstance(payload := row.get("payload"), dict)
        ]
        if (
            len(metadata) != 1
            or not isinstance(metadata[0].get("id"), str)
            or not metadata[0].get("id")
            or (sessions and metadata[0].get("id") not in sessions)
            or Path(metadata[0].get("cwd", "")).resolve() != root.resolve()
        ):
            problems.append(
                "Transcript identity does not match the callback session and scratch cwd."
            )
        elif path.stat().st_mtime < datetime.fromisoformat(manifest["started_at"]).timestamp():
            problems.append("Transcript predates this attempt.")
        else:
            version = metadata[0].get("cli_version")
            if (
                metadata[0].get("source") != "vscode"
                or metadata[0].get("originator") != "Codex Desktop"
            ):
                problems.append("Transcript does not establish the probed Codex desktop path.")
            if manifest["case"] in {"session-start-context", "user-prompt-context"} and any(
                row.get("type") == "response_item"
                and isinstance(payload := row.get("payload"), dict)
                and payload.get("type") in {"function_call", "custom_tool_call"}
                for row in rows
            ):
                problems.append("Context-only case used tools; token source is not isolated.")
            terminal_events = [
                payload.get("type")
                for row in rows
                if row.get("type") == "event_msg"
                and isinstance(payload := row.get("payload"), dict)
                and payload.get("type") in {"task_complete", "task_aborted"}
            ]
            completed = bool(terminal_events) and (
                terminal_events[-1] == "task_complete"
                or (
                    manifest["case"] == "interrupt-observe"
                    and any(row.get("event") == "Interrupt" for row in records)
                )
            )
            if manifest["case"] in {"pre-compact-context", "post-compact-context"}:
                boundaries = [i for i, row in enumerate(rows) if row.get("type") == "compacted"]
                after = rows[boundaries[0] + 1 :] if len(boundaries) == 1 else []
                answers = [
                    i
                    for i, row in enumerate(after)
                    if row.get("type") == "response_item"
                    and isinstance(payload := row.get("payload"), dict)
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                ]
                completions = [
                    i
                    for i, row in enumerate(after)
                    if row.get("type") == "event_msg"
                    and isinstance(payload := row.get("payload"), dict)
                    and payload.get("type") == "task_complete"
                ]
                if (
                    not observed_text(
                        "\n".join(map(json.dumps, after)), assistant_only=True
                    ).strip()
                    or not answers
                    or not completions
                    or completions[-1] <= answers[-1]
                    or any(
                        row.get("type") == "response_item"
                        and isinstance(payload := row.get("payload"), dict)
                        and payload.get("type") in {"function_call", "custom_tool_call"}
                        for row in rows
                    )
                ):
                    problems.append(
                        "Manual compaction and a completed isolated answer after it are required."
                    )
                raw = "\n".join(map(json.dumps, after))
            if manifest["case"] == "subagent-start-context":
                if (
                    len(child_inputs) != 1
                    or not isinstance(child_id := child_inputs[0].get("agent_id"), str)
                    or not child_id
                    or child_id in sessions
                    or not isinstance(child_path := child_inputs[0].get("transcript_path"), str)
                    or child_path in paths
                    or not isinstance(version, str)
                ):
                    problems.append("Exactly one callback-named child transcript is required.")
                else:
                    raw, child_problems = child_context(
                        Path(child_path),
                        child_id,
                        metadata[0]["id"],
                        root,
                        version,
                        manifest["started_at"],
                        rows,
                    )
                    problems.extend(child_problems)
                    # child_context already returns only verified assistant text.
                    raw = json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"text": raw}],
                            },
                        }
                    )
    else:
        problems.append("One same-session transcript path is required.")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        version = None
        problems.append("Backend version was not established from same-session metadata.")
    if problems:
        raw = ""
        completed = False
    return (
        probes.CodexEvidence(
            records=records,
            stdout="",
            rollout=observed_text(raw, assistant_only="compact" in manifest["case"]),
            files=frozenset(path.name for path in root.iterdir() if path.is_file()),
            completed=completed,
            file_markers=(
                frozenset()
                if manifest["case"] == "subagent-start-context" or "compact" in manifest["case"]
                else probes._codex_file_markers(root)
            ),
        ),
        version,
        problems,
    )


def observed_text(raw: str, *, assistant_only: bool = False) -> str:
    """Exclude user prompts, tool arguments and injected inputs from marker evidence."""
    observed: list[str] = []
    for row in probes.parse_stream(raw):
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            content = payload.get("content", [])
        elif not assistant_only and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            content = payload.get("output", [])
        else:
            continue
        if isinstance(content, str):
            observed.append(content)
        elif isinstance(content, list):
            observed.extend(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
    return "\n".join(observed)


def collect(root: Path, *, desktop_version: str, manual_transcript: Path | None = None) -> dict:
    if not VERSION.fullmatch(desktop_version):
        raise ValueError("record the exact numeric desktop app version from About")
    manifest = load_manifest(root)
    case = CASES[manifest["case"]]
    evidence, version, problems = read_evidence(root, manifest, manual_transcript)
    hook = root / ".codex/hooks.json"
    disabled = root / ".codex/hooks.json.wd153-disabled"
    if hook.exists() and digest(hook) == manifest["hook_sha256"] and not disabled.exists():
        hook.rename(disabled)
    elif not hook.exists() and disabled.exists() and digest(disabled) == manifest["hook_sha256"]:
        pass
    else:
        problems.append(
            "Owned hook definition changed or could not be disabled; inspect it manually."
        )
    verdict = ("inconclusive", problems) if problems else probes.evaluate_codex(case, evidence)
    record = cast(
        dict[str, Any],
        probes.codex_record(
            case,
            evidence,
            verdict=verdict,
            version=version,
            started_at=manifest["started_at"],
            finished_at=now(),
            removed=False,
        ),
    )
    record["record_id"] = f"wd153-codex-desktop-{case.name}-{manifest['run_id']}"
    record["provenance"]["provider"]["surface"] = "desktop"
    record["provenance"]["harness"] = {"name": "codex-desktop", "version": desktop_version}
    record["provenance"]["source"] = {
        "kind": "manual-observation",
        "sanitized_reference": f"wd153-{case.name}",
    }
    record["scope"]["isolation"] = "One scratch Git repository outside registered projects."
    record["scope"]["notes"] = (
        "Operator-driven desktop session; inherited global hooks may also run."
    )
    record["scope"]["expected_deliveries"] = 0
    record["stages"]["end_to_end_delivery"] = {
        "expected": 0,
        "persisted": 0,
        "read_back": 0,
        "state": "not_applicable",
        "failure_attribution": "not_applicable",
        "failure_detail": None,
        "evidence": "Direct provider probe; no Watchdog store or daemon delivery exercised.",
    }
    record["stages"]["provider_dispatch"].update(
        {
            "state": "confirmed" if evidence.completed else "unknown",
            "attempts": 1 if evidence.completed or evidence.records else 0,
            "evidence": "Same-session terminal-event read-back; collector launches no process.",
        }
    )
    record["cleanup"]["resources"] = [
        {
            "kind": "hook_installation",
            "alias": "scratch-project-hooks",
            "action": "removed" if disabled.exists() and not hook.exists() else "retained",
            "state": "confirmed" if disabled.exists() and not hook.exists() else "unknown",
            "failure_attribution": "not_applicable" if disabled.exists() else "unknown",
            "detail": "Owned hooks renamed to a disabled file; artifacts retained for review.",
            "evidence": "Owned hook hash and disabled-file check.",
        },
        {
            "kind": "temporary_state",
            "alias": "scratch-and-provider-transcript",
            "action": "retained",
            "state": "confirmed",
            "failure_attribution": "not_applicable",
            "detail": "Raw diagnostics intentionally retained; no recursive cleanup performed.",
            "evidence": "Collection reads scratch and transcript without deleting either.",
        },
        {
            "kind": "task",
            "alias": "operator-desktop-chat",
            "action": "retained",
            "state": "unknown",
            "failure_attribution": "unknown",
            "detail": None,
            "evidence": "Chat archival and process cleanup are not established by collection.",
        },
    ]
    record["cleanup"]["remaining_state"] = "Scratch folder and provider transcript retained."
    record["result"]["limitations"] = [
        note
        for note in record["result"]["limitations"]
        if note
        not in {
            "One Windows Codex CLI version and one scripted prompt.",
            "The non-interactive runner may not expose manual compaction.",
        }
    ] + [
        "App version/surface attributed by the operator; backend version from session metadata.",
        "One desktop/backend version and one case; no CLI or cross-version inheritance.",
        "Trust records are retained; no global hook or runtime control profile was changed.",
    ]
    # Unknown payload fields/tool names remain raw diagnostics only, never public evidence.
    for callback in record["capabilities"][case.name]["callbacks"].values():
        callback["input_fields"] = [
            key
            for key in callback["input_fields"]
            if key
            in {
                "hook_event_name",
                "session_id",
                "transcript_path",
                "cwd",
                "tool_name",
                "tool_input",
                "tool_response",
                "stop_hook_active",
                "model",
                "permission_mode",
                "turn_id",
                "agent_id",
            }
        ]
        callback["tool_names"] = [
            tool if tool in {"Bash", "apply_patch"} else "other" for tool in callback["tool_names"]
        ]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--case", choices=sorted(CASES), required=True)
    prep.add_argument("--config", type=Path)
    handler = commands.add_parser("handler")
    handler.add_argument("--root", type=Path, required=True)
    coll = commands.add_parser("collect")
    coll.add_argument("--root", type=Path, required=True)
    coll.add_argument("--desktop-version", required=True)
    coll.add_argument(
        "--transcript",
        type=Path,
        help="Same-scratch desktop rollout when callback discovery failed",
    )
    coll.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "handler":
        try:
            payload, _ = probes._read_hook_input()
            print(json.dumps(respond(args.root, payload)))
        except (OSError, ValueError, TypeError, KeyError):
            print("{}")
        return 0
    if args.action == "prepare":
        print(
            json.dumps(
                prepare(args.root, args.case, probes.registered_roots(args.config)), indent=2
            )
        )
    else:
        if args.output.resolve().is_relative_to(args.root.resolve()):
            parser.error("write sanitized evidence outside the scratch repository")
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            record = collect(
                args.root, desktop_version=args.desktop_version, manual_transcript=args.transcript
            )
            stream.write(json.dumps(record, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "case": next(iter(record["capabilities"])),
                    "state": next(iter(record["capabilities"].values()))["state"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
