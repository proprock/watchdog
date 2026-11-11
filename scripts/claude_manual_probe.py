"""Prepare and collect one interactive Claude CLI observation at a time.

This is a manual harness: the operator opens Claude in the prepared scratch Git
repository, follows the printed steps, then runs ``collect`` after the session
ends. It never reads or changes global Claude settings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_probe import (  # type: ignore[import-not-found]
    CLAUDE_CASES,
    DENY_REASON,
    SENTINEL,
    ClaudeCase,
    ClaudeEvidence,
    _append_log,
    _block_text,
    _no_response,
    _read_hook_input,
    _read_records,
    _read_rollout,
    _rollout_path,
    claude_record,
    ensure_scratch_outside_roots,
    parse_stream,
    registered_roots,
)

FORMAT_VERSION = "watchdog.claude-manual-kit.v1"
LOG_RELATIVE = Path(".wd153") / "probe.ndjson"
MANIFEST_RELATIVE = Path(".wd153") / "case.json"
SETTINGS_RELATIVE = Path(".claude") / "settings.local.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
WRITE_COMMAND = "echo WD139_PERMISSION > wd139_sentinel.txt"
ALLOWLIST = "Bash(echo *)"
INTERACTIVE_SCOPE_ID = "wd153-interactive-permission-v1"
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PermissionRequest"})


@dataclass(frozen=True)
class ManualCase:
    name: str
    base: str
    title: str
    prompt: str
    tool_command: str
    expected_events: tuple[str, ...]
    response_shape: str
    interaction: str


CASES: dict[str, ManualCase] = {
    "baseline": ManualCase(
        "baseline",
        "permission-observe",
        "Permission baseline",
        f"Run exactly this Bash command once: {WRITE_COMMAND}. Then say whether it ran "
        "or was refused, quoting any refusal text.",
        WRITE_COMMAND,
        ("PreToolUse", "PostToolUse"),
        "no decision response",
        "Do not approve or deny a dialog. The project-local tool allowlist should let the "
        "command run.",
    ),
    "pre-tool-ask": ManualCase(
        "pre-tool-ask",
        "pre-tool-ask",
        "Interactive PreToolUse ask",
        f"Run exactly this Bash command once: {WRITE_COMMAND}. Then say whether it ran "
        "or was refused, quoting any refusal text.",
        WRITE_COMMAND,
        ("PreToolUse", "PermissionRequest"),
        "hookSpecificOutput.permissionDecision=ask",
        "When Claude shows its real permission dialog, choose Deny. Do not type a denial "
        "into the prompt. Wait for Claude to report the refusal and finish the turn.",
    ),
    "permission-allow": ManualCase(
        "permission-allow",
        "permission-allow",
        "PermissionRequest allow",
        f"Run exactly this Bash command once: echo WD153_ALLOW > {SENTINEL}. "
        "Then say whether it ran.",
        f"echo WD153_ALLOW > {SENTINEL}",
        ("PermissionRequest",),
        "hookSpecificOutput.decision.behavior=allow",
        "Let the hook answer the permission request. Do not manually approve or deny it.",
    ),
    "permission-deny": ManualCase(
        "permission-deny",
        "permission-deny",
        "PermissionRequest deny",
        f"Run exactly this Bash command once: echo WD153_DENY > {SENTINEL}. "
        "Then say whether it ran or was refused, quoting any refusal text.",
        f"echo WD153_DENY > {SENTINEL}",
        ("PermissionRequest",),
        "hookSpecificOutput.decision.behavior=deny",
        "Let the hook answer the permission request. Do not manually approve or deny it.",
    ),
    "notification": ManualCase(
        "notification",
        "permission-observe",
        "Notification observation",
        f"Run exactly this Bash command once: echo WD153_NOTIFICATION > {SENTINEL}. "
        "Leave its permission dialog "
        "open until Claude reports a notification. Dismiss it and let the turn finish.",
        f"echo WD153_NOTIFICATION > {SENTINEL}",
        ("Notification",),
        "{} (observation only; no control response is claimed)",
        "Observe the actual notification and dismiss the pending dialog. A callback alone "
        "does not establish a control capability.",
    ),
    "notification-system-message": ManualCase(
        "notification-system-message",
        "permission-observe",
        "Notification systemMessage field",
        f"Run exactly this Bash command once: echo WD153_NOTIFICATION > {SENTINEL}. "
        "Leave its permission dialog open until Claude reports a notification. Dismiss it and "
        "then finish the requested work and report what happened.",
        f"echo WD153_NOTIFICATION > {SENTINEL}",
        ("Notification",),
        "systemMessage (single field; candidate only)",
        "Dismiss the real pending dialog and let Claude finish. The prompt does not contain "
        "the candidate system message text.",
    ),
    "notification-continue": ManualCase(
        "notification-continue",
        "permission-observe",
        "Notification continue field",
        f"Run exactly this Bash command once: echo WD153_NOTIFICATION > {SENTINEL}. "
        "Leave its permission dialog open until Claude reports a notification. Dismiss it and "
        "then finish the requested work and report what happened.",
        f"echo WD153_NOTIFICATION > {SENTINEL}",
        ("Notification",),
        "continue (single field; candidate only)",
        "Dismiss the real pending dialog and collect only after Claude produces a terminal turn.",
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def _safe_target(directory: Path) -> Path:
    expanded = directory.expanduser()
    # abspath normalizes without following symlinks; resolve() would follow them.
    absolute = Path(os.path.abspath(expanded))  # noqa: PTH100
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse(current):
            raise ValueError(f"refusing a scratch path through a reparse point: {current}")
    resolved = absolute.resolve(strict=False)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise ValueError("refusing a scratch path that resolves outside its requested location")
    return absolute


def _case_spec(case_name: str, directory: Path) -> tuple[ManualCase, ClaudeCase]:
    try:
        spec = CASES[case_name]
    except KeyError as error:
        raise ValueError(f"unknown manual case: {case_name}") from error
    base = CLAUDE_CASES[spec.base]
    events = [("UserPromptSubmit", None)]
    if case_name in {"baseline", "pre-tool-ask", "permission-allow", "permission-deny"}:
        source = CLAUDE_CASES["pre-tool-context"] if case_name == "baseline" else base
        events.extend((name, matcher) for name, matcher in source.events)
        if case_name == "baseline":
            events.extend((name, matcher) for name, matcher in base.events)
    elif case_name.startswith("notification"):
        events.append(("Notification", None))
    # Deduplicate event/matcher pairs while retaining the stable settings order.
    unique = tuple(dict.fromkeys(events))
    expected = {event: 1 for event in spec.expected_events}
    if case_name == "baseline":
        expected["PreToolUse"] = 1
        expected["PostToolUse"] = 1
        expected["PermissionRequest"] = 0
    if case_name == "pre-tool-ask":
        expected["PostToolUse"] = 0
    case = ClaudeCase(
        name=base.name,
        events=unique,
        expected=expected,
        responses=base.responses,
        response_shape=spec.response_shape,
        prompts=(spec.prompt,),
        judge=base.judge,
        tools=base.tools,
        allowed_tools=ALLOWLIST
        if case_name in {"baseline", "pre-tool-ask"}
        else base.allowed_tools,
        allow_error=True,
    )
    return spec, case


def _settings(case: ClaudeCase, directory: Path, case_name: str) -> dict[str, object]:
    from live_probe import hooks_settings  # type: ignore[import-not-found]

    handler = Path(__file__).resolve().as_posix()
    command = shlex.join(
        [
            sys.executable.replace("\\", "/"),
            handler,
            "handler",
            "--dir",
            directory.as_posix(),
            "--case",
            case_name,
        ]
    )
    settings = hooks_settings(case, command)
    if case_name in {"baseline", "pre-tool-ask"}:
        settings["permissions"] = {"allow": [ALLOWLIST]}
    return settings


def _scope_binding(spec: ManualCase) -> dict[str, str]:
    pair_case = spec.name in {"baseline", "pre-tool-ask"}
    workload = {
        "prompt": spec.prompt,
        "command": spec.tool_command,
        "sentinel": SENTINEL if pair_case else None,
        "tool_allowlist": [ALLOWLIST] if pair_case else [],
    }

    def canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    return {
        "case_id": INTERACTIVE_SCOPE_ID if pair_case else f"wd153-{spec.name}-v1",
        "workload_sha256": hashlib.sha256(canonical(workload)).hexdigest(),
        "settings_scope_sha256": hashlib.sha256(
            canonical({"project_permissions_allow": [ALLOWLIST] if pair_case else []})
        ).hexdigest(),
    }


def _same_directory(left: Path, right: Path) -> bool:
    try:
        left_path = _safe_target(left)
        right_path = _safe_target(right)
    except (OSError, ValueError):
        return False
    return os.path.normcase(str(left_path)) == os.path.normcase(str(right_path))


def _fixed_workload(payload: Mapping[str, object], expected_command: str) -> bool:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return (
        payload.get("tool_name") == "Bash"
        and isinstance(command, str)
        and command.rstrip() == expected_command
    )


def _settings_owned(path: Path, expected_hash: object) -> bool:
    if not isinstance(expected_hash, str) or _is_reparse(path) or not path.is_file():
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    except OSError:
        return False


def handle(directory: Path, case_name: str) -> int:
    """Run the fixed, fail-open hook handler for this one prepared case."""
    log: Path | None = None
    try:
        target = _safe_target(directory)
        log = target / LOG_RELATIVE
        spec = CASES[case_name]
        manifest = json.loads((target / MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        if (
            manifest.get("format_version") != FORMAT_VERSION
            or manifest.get("case") != case_name
            or manifest.get("scratch_root") != str(target)
        ):
            raise ValueError("hook directory does not match its prepared case")
        payload, event = _read_hook_input()
        case = _case_spec(case_name, target)[1]
        expected_events = {name for name, _matcher in case.events}
        cwd = payload.get("cwd")
        if (
            event not in expected_events
            or not isinstance(cwd, str)
            or not _same_directory(Path(cwd), target)
        ):
            print("{}")
            return 0
        if event == "UserPromptSubmit" and payload.get("prompt") != spec.prompt:
            print("{}")
            return 0
        if event in TOOL_EVENTS and not _fixed_workload(payload, spec.tool_command):
            print("{}")
            return 0
        base = CLAUDE_CASES[spec.base]
        response = base.responses.get(event, _no_response)(payload)
        if event == "Notification" and case_name == "notification-system-message":
            response = {
                "systemMessage": "After dismissal, reply exactly WD153_NOTIFICATION_CONTEXT."
            }
        elif event == "Notification" and case_name == "notification-continue":
            response = {"continue": False}
        _append_log(
            log,
            {"case": case_name, "event": event, "input": payload, "output": response},
        )
        print(json.dumps(response, sort_keys=True))
    except (OSError, KeyError, TypeError, ValueError):
        try:
            if log is not None:
                _append_log(log, {"case": case_name, "error": "handler_error"})
        except OSError:
            pass
        print("{}")
    return 0


def prepare(directory: Path, case_name: str, config: Path | None = None) -> dict[str, object]:
    """Create one isolated project and write only its project-local hook file."""
    target = _safe_target(directory)
    if target.exists() and any(target.iterdir()):
        raise ValueError(
            "scratch directory must be new or empty; each case needs a fresh directory"
        )
    ensure_scratch_outside_roots(target, registered_roots(config))
    spec, case = _case_spec(case_name, target)
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(target)], check=True, capture_output=True, text=True
    )
    (target / ".claude").mkdir()
    control = target / ".wd153"
    control.mkdir()
    settings_path = target / SETTINGS_RELATIVE
    settings_path.write_text(
        json.dumps(_settings(case, target, spec.name), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    settings_sha256 = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "case": spec.name,
        "created_at": _now(),
        "provider": "claude",
        "version": None,
        "version_required": "Record the exact `claude --version` output before collecting.",
        "prompt": spec.prompt,
        "interaction": spec.interaction,
        "expected_callback_kinds": list(spec.expected_events),
        "response_shape": spec.response_shape,
        "scope_binding": _scope_binding(spec),
        "settings_sha256": settings_sha256,
        "scratch_root": str(target),
        "cleanup": "Logs are retained by default. collect --disable-hook removes only "
        "this kit's .claude/settings.local.json after reading it.",
    }
    (target / MANIFEST_RELATIVE).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {
        "scratch": str(target),
        "case": spec.name,
        "settings": str(settings_path),
        "steps": [
            "Run `claude --version` and retain the exact output for collect --version.",
            f"Open a terminal in {target} and start `claude` interactively (no -p/headless mode).",
            f"Enter this prompt exactly: {spec.prompt}",
            spec.interaction,
            "Wait for the turn to finish, then exit Claude cleanly before collecting.",
            f"Run collect with --dir {target} and your exact Claude CLI version.",
        ],
    }


def _terminal_event(events: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """Return a real final assistant transcript event, never a synthesized result."""
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("stop_reason") in {"end_turn", "stop_sequence"}:
            return event
    return None


class ManualEvidence(ClaudeEvidence):
    terminal: Mapping[str, object] | None = None
    raw_events: Sequence[Mapping[str, object]] = ()

    def result(self) -> Mapping[str, object] | None:
        return self.terminal

    def reply(self) -> str:
        if self.terminal is None:
            return ""
        message = self.terminal.get("message")
        return _block_text(message.get("content")) if isinstance(message, dict) else ""

    def models(self) -> list[str]:
        models = {
            model
            for event in self.raw_events
            if isinstance(message := event.get("message"), dict)
            and isinstance(model := message.get("model"), str)
        }
        return sorted(models)


def _selected_session(records: Sequence[dict[str, object]]) -> str | None:
    sessions: list[str] = []
    for row in records:
        payload = row.get("input")
        session = payload.get("session_id") if isinstance(payload, dict) else None
        if (
            row.get("event") == "UserPromptSubmit"
            and isinstance(session, str)
            and session
            and session not in sessions
        ):
            sessions.append(session)
    if len(sessions) > 1:
        raise ValueError(
            "multiple Claude sessions used this scratch case; prepare a fresh directory"
        )
    return sessions[0] if sessions else None


def _session_records(records: Sequence[dict[str, object]], session: str) -> list[dict[str, object]]:
    return [
        row
        for row in records
        if isinstance(payload := row.get("input"), dict) and payload.get("session_id") == session
    ]


def _binding_issue(
    records: Sequence[dict[str, object]],
    transcript: Path,
    events: Sequence[Mapping[str, object]],
    session: str,
    target: Path,
    created_at: str,
) -> str | None:
    prompt_submits = [row for row in records if row.get("event") == "UserPromptSubmit"]
    if len(prompt_submits) != 1:
        return "exactly one UserPromptSubmit callback is required for support"
    transcript_sessions = {
        value for row in events if isinstance(value := row.get("sessionId"), str) and value
    }
    if transcript_sessions != {session}:
        return "transcript session identity does not match the bound callback session"
    for row in records:
        payload = row.get("input")
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if cwd is not None and (not isinstance(cwd, str) or not _same_directory(Path(cwd), target)):
            return "a callback cwd does not match the prepared scratch repository"
    for row in events:
        cwd = row.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not _same_directory(Path(cwd), target)):
            return "a transcript cwd does not match the prepared scratch repository"
    try:
        prepared_at = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        transcript_mtime = transcript.stat().st_mtime
    except (OSError, ValueError):
        return "transcript timestamp could not be verified against case preparation"
    if transcript_mtime < prepared_at:
        return "transcript predates case preparation"
    return None


def _notification_output(evidence: ManualEvidence) -> Mapping[str, object] | None:
    for row in reversed(evidence.records):
        if row.get("event") == "Notification" and isinstance(output := row.get("output"), dict):
            return output
    return None


def _workload_calls(evidence: ManualEvidence, event: str) -> list[Mapping[str, object]]:
    """Keep only callbacks for the fixed Bash command used by the pair cases."""
    matched: list[Mapping[str, object]] = []
    for payload in evidence.calls(event):
        if _fixed_workload(payload, WRITE_COMMAND):
            matched.append(payload)
    return matched


def _verdict(
    spec: ManualCase,
    evidence: ManualEvidence,
    operator_dialog: str | None,
    baseline_issue: str | None,
) -> tuple[str, list[str]]:
    observed = {row.get("event") for row in evidence.records}
    counts = {
        event: len(evidence.calls(event))
        for event in ("PreToolUse", "PostToolUse", "PermissionRequest", "Notification")
    }
    selected = {
        event: _workload_calls(evidence, event)
        for event in ("PreToolUse", "PostToolUse", "PermissionRequest")
    }
    if spec.name.startswith("notification"):
        if "Notification" not in observed:
            return "inconclusive", ["no Notification callback was observed in the bound session"]
        if spec.name == "notification":
            return "inconclusive", [
                "Notification callback observed; this callback does not establish "
                "a control capability"
            ]
        output = _notification_output(evidence)
        if output is None:
            return "inconclusive", ["the candidate Notification response was not recorded"]
        if spec.name == "notification-system-message":
            if set(output) != {"systemMessage"}:
                return "inconclusive", [
                    "the Notification callback did not return only systemMessage"
                ]
            if "WD153_NOTIFICATION_CONTEXT" in evidence.reply():
                return "supported", [
                    "the candidate systemMessage appeared in the same-session reply"
                ]
        elif spec.name == "notification-continue":
            if set(output) != {"continue"} or output.get("continue") is not False:
                return "inconclusive", [
                    "the Notification callback did not return only continue=false"
                ]
            if evidence.reply().strip():
                return "unsupported", [
                    "Claude completed and read back a same-session reply after continue=false"
                ]
        return "inconclusive", [
            "the same-session transcript did not establish candidate effect or countereffect"
        ]
    if spec.name == "baseline":
        if len(selected["PreToolUse"]) < 1 or len(selected["PostToolUse"]) < 1:
            return "inconclusive", ["baseline did not observe both PreToolUse and PostToolUse"]
        if counts["PermissionRequest"] != 0:
            return "inconclusive", [
                "baseline observed PermissionRequest; the allowlist did not avoid a dialog"
            ]
        if SENTINEL in evidence.files:
            return "supported", ["allowlisted baseline command ran without PermissionRequest"]
        return "inconclusive", ["baseline command did not create its scratch sentinel"]
    if spec.name == "pre-tool-ask":
        if baseline_issue is not None:
            return "inconclusive", [baseline_issue]
        if len(selected["PreToolUse"]) < 1 or len(selected["PermissionRequest"]) < 1:
            return "inconclusive", [
                "ask requires PreToolUse and PermissionRequest in the bound session"
            ]
        if operator_dialog != "denied":
            return "inconclusive", ["operator did not record denying the real Claude dialog"]
        if SENTINEL in evidence.files or selected["PostToolUse"]:
            return "unsupported", ["the denied command ran"]
        readback = " ".join(evidence.tool_results()).casefold()
        if not any(
            word in readback
            for word in ("denied", "rejected", "not approved", "permission declined")
        ):
            return "inconclusive", ["the same-session transcript did not read back the denial"]
        return "supported", ["interactive ask callback and denial read back in the bound session"]
    if spec.name == "permission-allow":
        if "PermissionRequest" not in observed:
            return "inconclusive", ["no PermissionRequest callback was observed"]
        if SENTINEL in evidence.files:
            return "supported", []
        return "inconclusive", [
            "PermissionRequest allow was observed but no scratch sentinel was written"
        ]
    if spec.name == "permission-deny":
        if "PermissionRequest" not in observed:
            return "inconclusive", ["no PermissionRequest callback was observed"]
        if SENTINEL in evidence.files:
            return "unsupported", ["the command ran despite the deny decision"]
        if any(DENY_REASON in text for text in evidence.tool_results()):
            return "supported", []
        return "inconclusive", ["command absence alone does not prove the denial was read back"]
    return "inconclusive", ["case has no manual judge"]


def _baseline_issue(path: Path | None, version: str, binding: Mapping[str, str]) -> str | None:
    if path is None:
        return "a supported baseline record is required to classify the interactive ask case"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "the linked baseline record could not be read"
    if not isinstance(record, dict):
        return "the linked baseline record is not an object"
    capabilities = record.get("capabilities")
    capability = capabilities.get("baseline") if isinstance(capabilities, dict) else None
    if not isinstance(capability, dict) or capability.get("state") != "supported":
        return "the linked baseline capability is not supported"
    provenance = record.get("provenance")
    provider = provenance.get("provider") if isinstance(provenance, dict) else None
    if not isinstance(provider, dict) or provider.get("version") != version:
        return "the linked baseline has a different Claude CLI version"
    scope = record.get("scope")
    if not isinstance(scope, dict) or scope.get("wd153") != dict(binding):
        return "the linked baseline workload or settings-scope hash does not match"
    if not str(record.get("record_id", "")).startswith("wd153-claude-manual-baseline-"):
        return "the linked record is not a WD-153 baseline"
    return None


def normalize_manual_record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Correct generic headless-runner metadata for an interactive manual session.

    This deliberately touches attribution and scope metadata only. The shared
    recorder remains unchanged for its headless callers.
    """
    record["scope"]["isolation"] = (
        "Scratch Git repository outside registered projects with project-local "
        ".claude/settings.local.json; ordinary user and project settings may also load."
    )
    record["result"]["limitations"] = [
        limitation
        for limitation in record["result"]["limitations"]
        if limitation != "One Windows Claude Code CLI version, haiku main model, one prompt."
    ]
    corrected_limit = (
        "One operator-run session and one prompt; CLI version was operator-reported from "
        "`claude --version`, not transcript-verified."
    )
    if corrected_limit not in record["result"]["limitations"]:
        record["result"]["limitations"].append(corrected_limit)
    resources = record["cleanup"]["resources"]
    if resources:
        resources[0]["detail"] = (
            "Project-local .claude/settings.local.json in scratch; CLI launched with "
            "ordinary settings discovery."
        )
    return record


def collect(
    directory: Path,
    *,
    version: str,
    operator_dialog: str | None = None,
    baseline: Path | None = None,
    disable_hook: bool = False,
) -> dict[str, object]:
    """Collect one bound session; only a real terminal completion can support a capability."""
    target = _safe_target(directory)
    manifest_path = target / MANIFEST_RELATIVE
    settings_path = target / SETTINGS_RELATIVE
    output_path = target / ".wd153" / "probe-result.v1.json"
    if output_path.exists():
        raise ValueError(
            "a result already exists for this case; collect into a fresh case directory"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION or manifest.get("scratch_root") != str(
        target
    ):
        raise ValueError("scratch manifest does not match this case directory")
    expected_settings_hash = manifest.get("settings_sha256")
    settings_issue: str | None = None
    if not _settings_owned(settings_path, expected_settings_hash):
        settings_issue = "project-local hook settings changed or disappeared; left untouched"
    version = version.strip()
    if not version or version.lower() in {"unknown", "n/a", "none"}:
        raise ValueError("an explicit Claude CLI version is required")
    spec = CASES[str(manifest["case"])]
    _, case = _case_spec(spec.name, target)
    if spec.name == "pre-tool-ask" and operator_dialog not in {"denied", "not-denied"}:
        raise ValueError("pre-tool-ask collection requires --operator-dialog denied or not-denied")
    log = target / LOG_RELATIVE
    logged = _read_records(log)
    session = _selected_session(logged)
    if session is None:
        raise ValueError("no UserPromptSubmit callback bound this scratch run to a Claude session")
    mine = _session_records(logged, session)
    transcript_paths = {
        Path(candidate).resolve()
        for row in mine
        if isinstance(payload := row.get("input"), dict)
        and isinstance(candidate := payload.get("transcript_path"), str)
        and candidate
    }
    if len(transcript_paths) != 1:
        raise ValueError("the bound Claude session must have exactly one transcript path")
    rollout = _rollout_path(mine)
    if rollout is None:
        raise ValueError("bound callbacks did not include the transcript path")
    raw_events = parse_stream(_read_rollout(rollout))
    terminal = _terminal_event(raw_events)
    terminal_issue = (
        None
        if terminal is not None
        else "same-session transcript has no real terminal assistant completion event"
    )
    files = frozenset(name for name in (entry.name for entry in target.iterdir()) if name != ".git")
    evidence = ManualEvidence(records=mine, stream=raw_events, files=files)
    evidence.raw_events = raw_events
    evidence.terminal = terminal
    binding = manifest.get("scope_binding")
    if not isinstance(binding, dict) or not all(
        isinstance(value, str) for value in binding.values()
    ):
        raise ValueError("scratch manifest has no valid scope binding")
    transcript_issue = _binding_issue(
        logged,
        rollout,
        raw_events,
        session,
        target,
        str(manifest["created_at"]),
    )
    baseline_issue = (
        _baseline_issue(baseline, version, binding) if spec.name == "pre-tool-ask" else None
    )
    baseline_record_id: str | None = None
    if spec.name == "pre-tool-ask" and baseline_issue is None and baseline is not None:
        baseline_record_id = json.loads(baseline.read_text(encoding="utf-8")).get("record_id")
    verdict = _verdict(spec, evidence, operator_dialog, baseline_issue)
    integrity_issue = settings_issue or transcript_issue
    if integrity_issue is not None:
        verdict = "inconclusive", [integrity_issue]
    elif terminal_issue is not None:
        verdict = "inconclusive", [terminal_issue]
    finished = _now()
    started = str(manifest["created_at"])
    removed = {"scratch": False}
    hook_disabled = False
    if disable_hook and settings_issue is None:
        # Recheck immediately before removal so an operator edit is preserved.
        if _settings_owned(settings_path, expected_settings_hash):
            settings_path.unlink()
            hook_disabled = not settings_path.exists()
        else:
            settings_issue = "project-local hook settings changed during cleanup; left untouched"
            verdict = "inconclusive", [settings_issue]
    record = normalize_manual_record_metadata(
        cast(
            dict[str, Any],
            claude_record(
                case,
                evidence,
                verdict=verdict,
                version=version,
                started_at=started,
                finished_at=finished,
                removed=removed,
                surface="cli-manual",
                record_name=spec.name,
            ),
        )
    )
    run_id = uuid.uuid4().hex
    record["record_id"] = f"wd153-claude-manual-{spec.name}-{run_id}"
    record["provenance"]["source"] = {
        "kind": "manual-observation",
        "sanitized_reference": f"wd153-{spec.name}",
    }
    record["scope"]["notes"] = (
        "Callbacks and transcript are bound to one session in this scratch repository. "
        "Prompt and transcript content are not copied into the record."
    )
    record["scope"]["wd153"] = binding
    if terminal_issue is not None:
        record["stages"]["provider_dispatch"].update(
            {
                "state": "incomplete",
                "evidence": "Same-session transcript has no terminal assistant completion event.",
                "detail": terminal_issue,
            }
        )
    if spec.name == "pre-tool-ask":
        record["scope"]["baseline_record_id"] = baseline_record_id
    if spec.name == "pre-tool-ask":
        record["capabilities"][spec.name]["operator_dialog"] = operator_dialog
    if disable_hook:
        hook_resource = record["cleanup"]["resources"][0]
        if hook_disabled:
            hook_resource["action"] = "removed"
            hook_resource["state"] = "confirmed"
            hook_resource["failure_attribution"] = "not_applicable"
        else:
            hook_resource["action"] = "retained"
            hook_resource["state"] = "unknown"
            hook_resource["failure_attribution"] = "unknown"
            hook_resource["detail"] = settings_issue
        record["cleanup"]["complete"] = False
    record["stages"]["persistence"] = {
        "accepted": 0,
        "losses": [],
        "evidence": "The manual provider probe does not exercise Watchdog persistence.",
    }
    record["stages"]["end_to_end_delivery"].update(
        {
            "persisted": 0,
            "read_back": 0,
            "state": "not_applicable",
            "failure_attribution": "not_applicable",
            "failure_detail": None,
            "evidence": "Manual provider callback and same-session transcript evidence only.",
        }
    )
    # The scratch repository is retained for review, so overall cleanup remains open.
    record["result"]["state"] = "incomplete"
    record["result"]["limitations"].append(
        "Watchdog persistence and end-to-end daemon delivery were not exercised."
    )
    if integrity_issue is not None:
        record["result"]["limitations"].append(integrity_issue)
    if settings_issue is not None and settings_issue != integrity_issue:
        record["result"]["limitations"].append(settings_issue)
    if terminal_issue is not None:
        record["result"]["limitations"].append(terminal_issue)
    result = {
        "record": record,
        "observed_session_bound": True,
        "terminal_completion": "confirmed" if terminal is not None else "not_confirmed",
        "hook_disabled": hook_disabled,
        "record_path": str(target / ".wd153" / "probe-result.v1.json"),
    }
    with output_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare", help="create a fresh one-case scratch Git repository")
    prep.add_argument("--dir", type=Path, required=True)
    prep.add_argument("--case", choices=sorted(CASES), required=True)
    prep.add_argument("--config", type=Path)
    gather = commands.add_parser("collect", help="collect a completed manual Claude session")
    gather.add_argument("--dir", type=Path, required=True)
    gather.add_argument("--version", required=True)
    gather.add_argument("--baseline", type=Path)
    gather.add_argument("--operator-dialog", choices=("denied", "not-denied"))
    gather.add_argument("--disable-hook", action="store_true")
    handler = commands.add_parser("handler", help="internal project-local Claude hook handler")
    handler.add_argument("--dir", type=Path, required=True)
    handler.add_argument("--case", choices=sorted(CASES), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "handler":
            return handle(args.dir, args.case)
        if args.action == "prepare":
            result = prepare(args.dir, args.case, args.config)
        else:
            result = collect(
                args.dir,
                version=args.version,
                operator_dialog=args.operator_dialog,
                baseline=args.baseline,
                disable_hook=args.disable_hook,
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
