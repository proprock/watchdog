"""Opt-in, isolated live probes for provider hook contracts (WD-139).

`codex-context` probes Codex CLI context delivery; `claude-control` probes Claude
Code hook controls (context, deny, rewrite, permission, stop/block, compaction,
Agent tool). Neither is called by the offline test suite or the normal Watchdog
command-line interface; both spend provider quota and need a standalone console.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.config import load_config, user_paths

USER_PROMPT_MARKER = "WD139_USER_PROMPT_CONTEXT"
POST_TOOL_MARKER = "WD139_POST_TOOL_CONTEXT"
EVENT_MARKERS = {
    "UserPromptSubmit": USER_PROMPT_MARKER,
    "PostToolUse": POST_TOOL_MARKER,
}
MAX_HOOK_INPUT_BYTES = 1024 * 1024
MAX_ROLLOUT_BYTES = 16 * 1024 * 1024


def hook_response(event: str) -> dict[str, object]:
    """Return the documented Codex additional-context response for one event."""
    try:
        marker = EVENT_MARKERS[event]
    except KeyError as error:
        raise ValueError(f"unsupported live-probe event: {event}") from error
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": marker,
        }
    }


def inside_registered_root(path: Path, roots: Sequence[Path]) -> bool:
    """Whether *path* resolves below one of the configured Watchdog projects."""
    try:
        candidate = path.resolve()
    except OSError:
        return True
    for root in roots:
        try:
            if candidate.is_relative_to(root.resolve()):
                return True
        except OSError:
            return True
    return False


def registered_roots(config_path: Path | None) -> list[Path]:
    """Read configured project roots without changing the user configuration."""
    config = load_config(config_path or user_paths().config)
    return [project.root for project in config.projects]


def ensure_scratch_outside_roots(scratch: Path, roots: Sequence[Path]) -> None:
    if inside_registered_root(scratch, roots):
        raise ValueError("refusing a scratch repository inside a registered Watchdog project")


def _append_log(log: Path, record: dict[str, object]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def _read_hook_input() -> tuple[dict[str, object], str]:
    raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("hook input exceeds the probe limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook input is not a JSON object")
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        raise ValueError("hook input has no event name")
    return payload, event


def handle(log: Path) -> int:
    """Record one raw scratch callback and print its documented JSON response."""
    try:
        payload, event = _read_hook_input()
        response = hook_response(event)
        _append_log(log, {"event": event, "input": payload, "output": response})
        print(json.dumps(response, sort_keys=True))
    except (OSError, TypeError, ValueError) as error:
        _append_log(log, {"error": type(error).__name__})
        print("{}")
    return 0


def command_windows(python: Path, handler: Path, log: Path) -> str:
    arguments = [str(python), str(handler), "handler", "--log", str(log)]
    return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)


def profile_configuration(handler: Path, log: Path) -> str:
    """Render a temporary user-level profile so scratch trust cannot skip hooks."""
    command = shlex.join([sys.executable, str(handler), "handler", "--log", str(log)])
    command_windows_value = command_windows(Path(sys.executable), handler, log)
    fields = [
        'type = "command"',
        f"command = {json.dumps(command)}",
        "timeout = 30",
        "additionalContextLimit = 128",
    ]
    if os.name == "nt":
        fields.insert(2, f"commandWindows = {json.dumps(command_windows_value)}")
    return "\n".join(
        [
            "[[hooks.UserPromptSubmit]]",
            "",
            "[[hooks.UserPromptSubmit.hooks]]",
            *fields,
            "",
            "[[hooks.PostToolUse]]",
            'matcher = "^Bash$"',
            "",
            "[[hooks.PostToolUse.hooks]]",
            *fields,
            "",
        ]
    )


def codex_command(codex: str, profile_name: str, scratch: Path, prompt: str) -> list[str]:
    return [
        codex,
        "exec",
        "--enable",
        "hooks",
        "--profile",
        profile_name,
        "--dangerously-bypass-hook-trust",
        "--sandbox",
        "read-only",
        "-C",
        str(scratch),
        prompt,
    ]


def _read_records(log: Path) -> list[dict[str, object]]:
    if not log.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _rollout_path(records: Sequence[dict[str, object]]) -> Path | None:
    for record in records:
        payload = record.get("input")
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("transcript_path")
        if isinstance(candidate, str) and candidate:
            return Path(candidate)
    return None


def _read_rollout(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_ROLLOUT_BYTES + 1).decode("utf-8", errors="replace")
    except OSError:
        return ""


def verify(
    records: Sequence[dict[str, object]], stdout: str, rollout: str
) -> tuple[str, list[str]]:
    """Classify only the evidence that this one probe actually collected."""
    observed: dict[str, int] = {event: 0 for event in EVENT_MARKERS}
    returned: set[str] = set()
    for record in records:
        event = record.get("event")
        if not isinstance(event, str) or event not in EVENT_MARKERS:
            continue
        observed[event] += 1
        output = record.get("output")
        if not isinstance(output, dict):
            continue
        specific = output.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            continue
        if (
            specific.get("hookEventName") == event
            and specific.get("additionalContext") == EVENT_MARKERS[event]
        ):
            returned.add(event)

    reasons = [f"missing callback: {event}" for event, count in observed.items() if count != 1]
    reasons.extend(
        f"missing expected response: {event}" for event in EVENT_MARKERS if event not in returned
    )
    for marker in EVENT_MARKERS.values():
        if marker not in stdout:
            reasons.append(f"marker absent from Codex response: {marker}")
        if marker not in rollout:
            reasons.append(f"marker absent from rollout: {marker}")
    return ("supported" if not reasons else "inconclusive", reasons)


def _result(
    *,
    version: str | None,
    records: Sequence[dict[str, object]],
    state: str,
    limitations: Sequence[str],
    exit_code: int | None,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    callbacks: dict[str, dict[str, object]] = {}
    for event in EVENT_MARKERS:
        event_records = [record for record in records if record.get("event") == event]
        input_fields = {
            key
            for record in event_records
            for payload in [record.get("input")]
            if isinstance(payload, dict)
            for key in payload
            if isinstance(key, str)
        }
        callbacks[event] = {
            "observed": len(event_records),
            "input_fields": sorted(input_fields),
            "response": hook_response(event),
        }
    return {
        "format_version": "watchdog.wd139-codex-context.v1",
        "provenance": {
            "provider": "codex",
            "version": version,
            "surface": "cli",
            "os": {"name": platform.system(), "architecture": platform.machine()},
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "callbacks": callbacks,
        "result": {"state": state, "limitations": list(limitations)},
        "cleanup": {
            "scratch_repository": "removed",
            "temporary_profile": "removed",
            "user_configuration": "unchanged",
        },
        "codex_exit_code": exit_code,
    }


def _resolve_executable(command: str, label: str, option: str) -> str:
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"could not find {label}; add {command} to PATH or pass {option} PATH")
    return resolved


def resolve_codex(command: str) -> str:
    return _resolve_executable(command, "Codex CLI", "--codex")


def codex_version(codex: str) -> str:
    result = subprocess.run(
        [codex, "--version"],
        capture_output=True,
        check=True,
        text=True,
        creationflags=hidden_creationflags(),
    )
    return result.stdout.strip()


def require_standalone_cli() -> None:
    if os.environ.get("CODEX_APP_TOOLS_PIPE_PATH") or os.environ.get("CODEX_THREAD_ID"):
        raise RuntimeError("run the live probe from a standalone PowerShell or cmd.exe console")


def run_codex_context(args: argparse.Namespace) -> dict[str, object]:
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    require_standalone_cli()
    roots = registered_roots(args.config)
    temporary_parent = Path(tempfile.gettempdir())
    ensure_scratch_outside_roots(temporary_parent, roots)
    codex = resolve_codex(args.codex)
    version = codex_version(codex)
    with tempfile.TemporaryDirectory(prefix="watchdog-wd139-codex-") as temporary:
        scratch = Path(temporary)
        ensure_scratch_outside_roots(scratch, roots)
        log = scratch / "probe.ndjson"
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        profile_name = f"watchdog-wd139-{uuid4().hex}"
        profile = home / f"{profile_name}.config.toml"
        if profile.exists():
            raise RuntimeError("unexpected existing temporary Codex probe profile")
        profile.write_text(
            profile_configuration(Path(__file__).resolve(), log), encoding="utf-8", newline="\n"
        )
        prompt = (
            "Run exactly one Bash command: echo WD139_SAFE_TOOL. After it completes, reply with "
            "the two hook-injected tokens, one per line, and nothing else. Do not guess tokens "
            "that you have not received."
        )
        try:
            subprocess.run(
                ["git", "init", str(scratch)],
                capture_output=True,
                text=True,
                check=True,
                creationflags=hidden_creationflags(),
            )
            command = codex_command(codex, profile_name, scratch, prompt)
            completed: subprocess.CompletedProcess[str] | None = None
            failure: str | None = None
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    creationflags=hidden_creationflags(),
                )
            except subprocess.TimeoutExpired:
                failure = "Codex timed out before the probe completed"
            records = _read_records(log)
            rollout = _read_rollout(_rollout_path(records))
            stdout = completed.stdout if completed is not None else ""
            state, limitations = verify(records, stdout, rollout)
            if failure is not None:
                limitations.append(failure)
            if completed is not None and completed.returncode != 0:
                limitations.append("Codex exited nonzero")
            if limitations:
                state = "inconclusive"
            return _result(
                version=version,
                records=records,
                state=state,
                limitations=limitations,
                exit_code=completed.returncode if completed is not None else None,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        finally:
            profile.unlink(missing_ok=True)


# --- Claude Code control probes ---------------------------------------------------
#
# One `claude -p` run per case, each with its own scratch repository and a
# `--settings` file holding only that case's hooks. `--setting-sources ""` drops the
# user's and project's settings so the installed Watchdog hooks cannot fire, and
# `--safe-mode`/`--bare` are not used because they would disable the probe hooks too.
# Read-back comes from the `stream-json` output, so no transcript is persisted.

CTX = "WD139_CTX_"
SAFE_COMMAND = "echo WD139_SAFE_TOOL"
DENY_REASON = "WD139_DENY_REASON"
REWRITTEN = "WD139_REWRITTEN"
ORIGINAL = "WD139_ORIGINAL"
STOP_ACK = "WD139_STOP_ACK"
FINISHED = "WD139_FINISHED"
SENTINEL = "wd139_sentinel.txt"
AGENT_MATCHER = "Agent|Task"
NO_TOKEN = re.compile(r"\bNONE\b")

Response = Callable[[Mapping[str, object]], dict[str, object]]
Verdict = tuple[str, list[str]]


@dataclass(frozen=True)
class ClaudeEvidence:
    """Everything one case collected: hook log, `stream-json` events, scratch file names."""

    records: Sequence[Mapping[str, object]]
    stream: Sequence[Mapping[str, object]]
    files: frozenset[str] = frozenset()
    timed_out: bool = False

    def calls(self, event: str) -> list[Mapping[str, object]]:
        found: list[Mapping[str, object]] = []
        for record in self.records:
            payload = record.get("input")
            if record.get("event") == event and isinstance(payload, dict):
                found.append(payload)
        return found

    def result(self) -> Mapping[str, object] | None:
        for event in reversed(self.stream):
            if event.get("type") == "result":
                return event
        return None

    def reply(self) -> str:
        result = self.result()
        text = result.get("result") if result is not None else None
        return text if isinstance(text, str) else ""

    def tool_results(self) -> list[str]:
        texts: list[str] = []
        for event in self.stream:
            message = event.get("message") if event.get("type") == "user" else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    texts.append(_block_text(block.get("content")))
        return texts

    def models(self) -> list[str]:
        result = self.result()
        usage = result.get("modelUsage") if result is not None else None
        return sorted(usage) if isinstance(usage, dict) else []

    def compacted(self) -> bool:
        return any(
            event.get("type") == "system" and event.get("subtype") == "compact_boundary"
            for event in self.stream
        )

    def saw(self, text: str) -> bool:
        return text in self.reply() or any(text in result for result in self.tool_results())


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


@dataclass(frozen=True)
class ClaudeCase:
    """One isolated capability. `expected` is the minimum callback count per event."""

    name: str
    events: tuple[tuple[str, str | None], ...]
    expected: Mapping[str, int]
    responses: Mapping[str, Response]
    response_shape: str
    prompts: tuple[str, ...]
    judge: Callable[[ClaudeEvidence], Verdict]
    tools: str = "Bash"
    allowed_tools: str | None = "Bash(echo *)"
    allow_error: bool = False


def _no_response(_payload: Mapping[str, object]) -> dict[str, object]:
    return {}


def _context(event: str, marker: str) -> Response:
    def respond(_payload: Mapping[str, object]) -> dict[str, object]:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": marker}}

    return respond


def _pre_tool(decision: str, reason: str, **updated: str) -> Response:
    def respond(payload: Mapping[str, object]) -> dict[str, object]:
        output: dict[str, object] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
        if updated:
            tool_input = payload.get("tool_input")
            output["updatedInput"] = {
                **(tool_input if isinstance(tool_input, dict) else {}),
                **updated,
            }
        return {"hookSpecificOutput": output}

    return respond


def _permission(behavior: str) -> Response:
    def respond(_payload: Mapping[str, object]) -> dict[str, object]:
        decision: dict[str, object] = {"behavior": behavior}
        if behavior == "deny":
            decision["message"] = DENY_REASON
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    return respond


def _block(reason: str) -> Response:
    def respond(_payload: Mapping[str, object]) -> dict[str, object]:
        return {"decision": "block", "reason": reason}

    return respond


def _block_once(reason: str) -> Response:
    """Block the first Stop only; `stop_hook_active` guards against a loop."""

    def respond(payload: Mapping[str, object]) -> dict[str, object]:
        return (
            {}
            if payload.get("stop_hook_active") is True
            else {"decision": "block", "reason": reason}
        )

    return respond


def _halt(_payload: Mapping[str, object]) -> dict[str, object]:
    return {"continue": False, "stopReason": "WD139_HALT"}


def _context_verdict(marker: str, event: str) -> Callable[[ClaudeEvidence], Verdict]:
    def judge(evidence: ClaudeEvidence) -> Verdict:
        if marker in evidence.reply():
            return "supported", []
        if NO_TOKEN.search(evidence.reply()):
            return "unsupported", [f"{event} returned context but the model reported none"]
        return "inconclusive", ["the reply neither repeated the token nor reported none"]

    return judge


def _blocked_verdict(ran: Callable[[ClaudeEvidence], bool]) -> Callable[[ClaudeEvidence], Verdict]:
    def judge(evidence: ClaudeEvidence) -> Verdict:
        if ran(evidence):
            return "unsupported", ["the tool ran despite the deny response"]
        if evidence.saw(DENY_REASON):
            return "supported", []
        return "inconclusive", ["the tool did not run but the deny reason never reached the model"]

    return judge


def _bash_ran(evidence: ClaudeEvidence) -> bool:
    return SENTINEL in evidence.files or bool(evidence.calls("PostToolUse"))


def _rewrite_verdict(evidence: ClaudeEvidence) -> Verdict:
    results = evidence.tool_results()
    if any(REWRITTEN in text for text in results):
        return "supported", []
    if any(ORIGINAL in text for text in results):
        return "unsupported", ["the original command ran; updatedInput was ignored"]
    return "inconclusive", ["no tool result showed either command"]


def _permission_observed(evidence: ClaudeEvidence) -> Verdict:
    ran = SENTINEL in evidence.files
    return "supported", [f"headless baseline: the command {'ran' if ran else 'was refused'}"]


def _permission_allow(evidence: ClaudeEvidence) -> Verdict:
    if SENTINEL in evidence.files:
        return "supported", []
    return "unsupported", ["PermissionRequest fired and allowed, but the command did not run"]


def _permission_deny(evidence: ClaudeEvidence) -> Verdict:
    if SENTINEL in evidence.files:
        return "unsupported", ["the command ran despite the deny decision"]
    if evidence.saw(DENY_REASON):
        return "supported", []
    return "inconclusive", ["the command did not run but the deny message never reached the model"]


def _stop_block_verdict(event: str) -> Callable[[ClaudeEvidence], Verdict]:
    def judge(evidence: ClaudeEvidence) -> Verdict:
        stops = evidence.calls(event)
        if len(stops) < 2:
            return "unsupported", [f"{event} fired once: the block did not continue the work"]
        if not any(stop.get("stop_hook_active") is True for stop in stops):
            return "inconclusive", [f"no later {event} carried stop_hook_active"]
        if STOP_ACK in evidence.reply():
            return "supported", []
        return "inconclusive", ["the work continued but the block reason was not acted on"]

    return judge


def _ask_verdict(evidence: ClaudeEvidence) -> Verdict:
    """`ask` must stop an allowlisted command that runs freely without the hook."""
    if evidence.calls("PostToolUse"):
        return "unsupported", ["the allowlisted command ran: ask was ignored"]
    if evidence.calls("PermissionRequest"):
        return "supported", [
            "ask handed the call to the permission flow (a PermissionRequest followed)"
        ]
    return "supported", [
        "headless: ask refuses without a host and fires no PermissionRequest; "
        "an interactive prompt is not exercised"
    ]


def _observed_verdict(evidence: ClaudeEvidence) -> Verdict:
    return "supported", ["callback delivery only; the event defines no response field"]


def _halt_verdict(evidence: ClaudeEvidence) -> Verdict:
    if FINISHED in evidence.reply():
        return "unsupported", ["the model kept going after continue:false"]
    return "supported", ["evidence is the absence of the post-tool reply"]


def _compact_block_verdict(evidence: ClaudeEvidence) -> Verdict:
    if evidence.compacted():
        return "unsupported", ["compaction completed despite the block"]
    return "supported", ["evidence is the absence of a compact_boundary event"]


def _compact_context_verdict(evidence: ClaudeEvidence) -> Verdict:
    if not evidence.compacted():
        return "inconclusive", ["compaction did not run"]
    return _context_verdict(CTX + "PRE_COMPACT", "PreCompact")(evidence)


def _agent_ran(evidence: ClaudeEvidence) -> bool:
    return bool(evidence.calls("SubagentStart"))


def _agent_rewrite_verdict(evidence: ClaudeEvidence) -> Verdict:
    if not _agent_ran(evidence):
        return "inconclusive", ["no subagent started"]
    if any(model.startswith("claude-sonnet") for model in evidence.models()):
        return "supported", []
    return "unsupported", ["the subagent never used the rewritten model"]


_TOKEN_ASK = (
    "Reply with the hook-injected token that starts with WD139_CTX_ and nothing else; "
    "reply NONE if you received no such token. Do not guess."
)
_BASH_THEN_TOKEN = (
    f"Run exactly one Bash command: {SAFE_COMMAND}. After it completes, "
    f"{_TOKEN_ASK[0].lower()}{_TOKEN_ASK[1:]}"
)
_WRITE_COMMAND = f"echo WD139_PERMISSION > {SENTINEL}"
_PERMISSION_PROMPT = (
    f"Run exactly one Bash command: {_WRITE_COMMAND}. Then state in one sentence whether "
    "it ran or was refused, quoting any refusal text."
)
_AGENT_PROMPT = (
    'Call the Agent tool exactly once with subagent_type "general-purpose", model "haiku", '
    'description "probe" and prompt "Reply with the single word ok". Then state in one '
    "sentence whether the call succeeded or why it was refused, quoting any refusal text."
)
_SUBAGENT_RELAY = (
    'Call the Agent tool exactly once with subagent_type "general-purpose", model "haiku", '
    'description "probe" and prompt "{task}" Then reply with the subagent\'s answer verbatim '
    "and nothing else."
)
_PRE_TOOL_BASH = (("PreToolUse", "Bash"), ("PostToolUse", "Bash"))


CLAUDE_CASES: dict[str, ClaudeCase] = {
    case.name: case
    for case in [
        ClaudeCase(
            name="session-start-context",
            events=(("SessionStart", None),),
            expected={"SessionStart": 1},
            responses={"SessionStart": _context("SessionStart", CTX + "SESSION_START")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=(_TOKEN_ASK,),
            judge=_context_verdict(CTX + "SESSION_START", "SessionStart"),
            tools="",
            allowed_tools=None,
        ),
        ClaudeCase(
            name="user-prompt-context",
            events=(("UserPromptSubmit", None),),
            expected={"UserPromptSubmit": 1},
            responses={"UserPromptSubmit": _context("UserPromptSubmit", CTX + "USER_PROMPT")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=(_TOKEN_ASK,),
            judge=_context_verdict(CTX + "USER_PROMPT", "UserPromptSubmit"),
            tools="",
            allowed_tools=None,
        ),
        ClaudeCase(
            name="pre-tool-context",
            events=_PRE_TOOL_BASH,
            expected={"PreToolUse": 1},
            responses={"PreToolUse": _context("PreToolUse", CTX + "PRE_TOOL")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=(_BASH_THEN_TOKEN,),
            judge=_context_verdict(CTX + "PRE_TOOL", "PreToolUse"),
        ),
        ClaudeCase(
            name="post-tool-context",
            events=_PRE_TOOL_BASH,
            expected={"PostToolUse": 1},
            responses={"PostToolUse": _context("PostToolUse", CTX + "POST_TOOL")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=(_BASH_THEN_TOKEN,),
            judge=_context_verdict(CTX + "POST_TOOL", "PostToolUse"),
        ),
        ClaudeCase(
            name="pre-tool-deny",
            events=_PRE_TOOL_BASH,
            expected={"PreToolUse": 1},
            responses={"PreToolUse": _pre_tool("deny", DENY_REASON)},
            response_shape="hookSpecificOutput.permissionDecision=deny",
            prompts=(_PERMISSION_PROMPT,),
            judge=_blocked_verdict(_bash_ran),
        ),
        ClaudeCase(
            name="pre-tool-rewrite",
            events=_PRE_TOOL_BASH,
            expected={"PreToolUse": 1},
            responses={
                "PreToolUse": _pre_tool(
                    "allow", "WD-139 rewrite probe", command=f"echo {REWRITTEN}"
                )
            },
            response_shape="hookSpecificOutput.updatedInput",
            prompts=(f"Run exactly one Bash command: echo {ORIGINAL}. Then quote its output.",),
            judge=_rewrite_verdict,
        ),
        ClaudeCase(
            name="permission-observe",
            events=(("PermissionRequest", "Bash"),),
            expected={"PermissionRequest": 1},
            responses={},
            response_shape="{}",
            prompts=(_PERMISSION_PROMPT,),
            judge=_permission_observed,
            allowed_tools=None,
        ),
        ClaudeCase(
            name="permission-allow",
            events=(("PermissionRequest", "Bash"),),
            expected={"PermissionRequest": 1},
            responses={"PermissionRequest": _permission("allow")},
            response_shape="hookSpecificOutput.decision.behavior=allow",
            prompts=(_PERMISSION_PROMPT,),
            judge=_permission_allow,
            allowed_tools=None,
        ),
        ClaudeCase(
            name="permission-deny",
            events=(("PermissionRequest", "Bash"),),
            expected={"PermissionRequest": 1},
            responses={"PermissionRequest": _permission("deny")},
            response_shape="hookSpecificOutput.decision.behavior=deny",
            prompts=(_PERMISSION_PROMPT,),
            judge=_permission_deny,
            allowed_tools=None,
        ),
        ClaudeCase(
            name="stop-block",
            events=(("Stop", None),),
            expected={"Stop": 1},
            responses={"Stop": _block_once(f"Reply with exactly {STOP_ACK} and nothing else.")},
            response_shape="decision=block",
            prompts=("Reply with the single word READY.",),
            judge=_stop_block_verdict("Stop"),
            tools="",
            allowed_tools=None,
        ),
        ClaudeCase(
            name="pre-tool-ask",
            events=(*_PRE_TOOL_BASH, ("PermissionRequest", "Bash")),
            expected={"PreToolUse": 1},
            responses={"PreToolUse": _pre_tool("ask", "WD-139 ask probe")},
            response_shape="hookSpecificOutput.permissionDecision=ask",
            prompts=(
                f"Run exactly one Bash command: {SAFE_COMMAND}. Then state in one sentence "
                "whether it ran or was refused, quoting any refusal text.",
            ),
            judge=_ask_verdict,
        ),
        ClaudeCase(
            name="subagent-start-context",
            events=(("SubagentStart", None),),
            expected={"SubagentStart": 1},
            responses={"SubagentStart": _context("SubagentStart", CTX + "SUBAGENT_START")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=(_SUBAGENT_RELAY.format(task=_TOKEN_ASK),),
            judge=_context_verdict(CTX + "SUBAGENT_START", "SubagentStart"),
            tools="Agent",
            allowed_tools="Agent",
        ),
        ClaudeCase(
            name="subagent-stop-block",
            events=(("SubagentStop", None),),
            expected={"SubagentStop": 1},
            responses={
                "SubagentStop": _block_once(f"Reply with exactly {STOP_ACK} and nothing else.")
            },
            response_shape="decision=block",
            prompts=(_SUBAGENT_RELAY.format(task="Reply with the single word ok."),),
            judge=_stop_block_verdict("SubagentStop"),
            tools="Agent",
            allowed_tools="Agent",
        ),
        ClaudeCase(
            name="session-end-observe",
            events=(("SessionEnd", None),),
            expected={"SessionEnd": 1},
            responses={},
            response_shape="{}",
            prompts=("Reply with the single word READY.",),
            judge=_observed_verdict,
            tools="",
            allowed_tools=None,
        ),
        ClaudeCase(
            name="post-tool-block",
            events=_PRE_TOOL_BASH,
            expected={"PostToolUse": 1},
            responses={"PostToolUse": _block(f"Report the token {CTX}POST_BLOCK to the user.")},
            response_shape="decision=block",
            prompts=(_BASH_THEN_TOKEN,),
            judge=_context_verdict(CTX + "POST_BLOCK", "PostToolUse"),
        ),
        ClaudeCase(
            name="post-tool-halt",
            events=_PRE_TOOL_BASH,
            expected={"PostToolUse": 1},
            responses={"PostToolUse": _halt},
            response_shape="continue=false",
            prompts=(
                f"Run exactly one Bash command: {SAFE_COMMAND}. After it completes, reply "
                f"with the exact word {FINISHED}.",
            ),
            judge=_halt_verdict,
        ),
        ClaudeCase(
            name="pre-compact-block",
            events=(("PreCompact", None), ("PostCompact", None)),
            expected={"PreCompact": 1},
            responses={"PreCompact": _block("WD139_COMPACT_BLOCK")},
            response_shape="decision=block",
            prompts=("Reply with the single word READY.", "/compact"),
            judge=_compact_block_verdict,
            tools="",
            allowed_tools=None,
            allow_error=True,
        ),
        ClaudeCase(
            name="pre-compact-context",
            events=(("PreCompact", None), ("PostCompact", None)),
            expected={"PreCompact": 1},
            responses={"PreCompact": _context("PreCompact", CTX + "PRE_COMPACT")},
            response_shape="hookSpecificOutput.additionalContext",
            prompts=("Reply with the single word READY.", "/compact", _TOKEN_ASK),
            judge=_compact_context_verdict,
            tools="",
            allowed_tools=None,
        ),
        ClaudeCase(
            name="agent-deny",
            events=(("PreToolUse", AGENT_MATCHER), ("SubagentStart", None)),
            expected={"PreToolUse": 1},
            responses={"PreToolUse": _pre_tool("deny", DENY_REASON)},
            response_shape="hookSpecificOutput.permissionDecision=deny",
            prompts=(_AGENT_PROMPT,),
            judge=_blocked_verdict(_agent_ran),
            tools="Agent",
            allowed_tools="Agent",
        ),
        ClaudeCase(
            name="agent-rewrite",
            events=(("PreToolUse", AGENT_MATCHER), ("SubagentStart", None)),
            expected={"PreToolUse": 1},
            responses={"PreToolUse": _pre_tool("allow", "WD-139 rewrite probe", model="sonnet")},
            response_shape="hookSpecificOutput.updatedInput.model",
            prompts=(_AGENT_PROMPT,),
            judge=_agent_rewrite_verdict,
            tools="Agent",
            allowed_tools="Agent",
        ),
    ]
}


def handle_claude(log: Path, case_name: str) -> int:
    """Record one scratch callback and answer it with the case's response."""
    try:
        payload, event = _read_hook_input()
        response = CLAUDE_CASES[case_name].responses.get(event, _no_response)(payload)
        _append_log(log, {"event": event, "input": payload, "output": response})
        print(json.dumps(response, sort_keys=True))
    except (OSError, KeyError, TypeError, ValueError) as error:
        _append_log(log, {"error": type(error).__name__})
        print("{}")
    return 0


def claude_settings(case: ClaudeCase, handler: Path, log: Path) -> dict[str, object]:
    """Render a settings file whose only hooks are this case's, as POSIX paths for Git Bash."""
    command = shlex.join(
        [
            Path(sys.executable).as_posix(),
            handler.as_posix(),
            "claude-handler",
            "--log",
            log.as_posix(),
            "--case",
            case.name,
        ]
    )
    hooks: dict[str, list[dict[str, object]]] = {}
    for event, matcher in case.events:
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 30}]
        }
        if matcher is not None:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    return {"hooks": hooks}


def claude_command(claude: str, case: ClaudeCase, settings: Path, budget: float) -> list[str]:
    command = [
        claude,
        "-p",
        "--setting-sources",
        "",
        "--settings",
        str(settings),
        "--strict-mcp-config",
        "--model",
        "haiku",
        "--tools",
        case.tools,
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--max-budget-usd",
        str(budget),
    ]
    if case.allowed_tools is not None:
        command += ["--allowedTools", case.allowed_tools]
    if len(case.prompts) > 1:
        command += ["--input-format", "stream-json"]
    return command


def claude_stdin(case: ClaudeCase) -> str:
    """One prompt as text, or several as `stream-json` user messages (compaction needs history)."""
    if len(case.prompts) == 1:
        return case.prompts[0]
    return "".join(
        json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
        for prompt in case.prompts
    )


def parse_stream(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _problems(case: ClaudeCase, evidence: ClaudeEvidence) -> list[str]:
    reasons: list[str] = []
    if evidence.timed_out:
        reasons.append("Claude timed out before the probe completed")
    result = evidence.result()
    if result is None:
        reasons.append("no result event in the Claude stream")
    elif result.get("is_error") is True and not case.allow_error:
        reasons.append(f"Claude reported an error ({result.get('terminal_reason')})")
    for event, minimum in case.expected.items():
        if len(evidence.calls(event)) < minimum:
            reasons.append(f"missing callback: {event}")
    return reasons


def evaluate(case: ClaudeCase, evidence: ClaudeEvidence) -> Verdict:
    """Classify as supported, unsupported (ran cleanly, effect absent) or inconclusive."""
    reasons = _problems(case, evidence)
    if reasons:
        return "inconclusive", reasons
    return case.judge(evidence)


def claude_record(
    case: ClaudeCase,
    evidence: ClaudeEvidence,
    *,
    verdict: Verdict,
    version: str | None,
    started_at: str,
    finished_at: str,
    removed: Mapping[str, bool],
    surface: str = "cli",
) -> dict[str, object]:
    """Build one `watchdog.probe-result.v1` record that keeps only shapes, never content."""
    state, notes = verdict
    desktop = surface == "desktop"
    expected = sum(case.expected.values())
    observed = sum(min(len(evidence.calls(event)), count) for event, count in case.expected.items())
    result = evidence.result()
    dispatched = result is not None and not evidence.timed_out
    callbacks: dict[str, object] = {}
    for event, _matcher in case.events:
        inputs = evidence.calls(event)
        callbacks[event] = {
            "observed": len(inputs),
            "input_fields": sorted({key for item in inputs for key in item}),
            "tool_names": sorted(
                {name for item in inputs if isinstance(name := item.get("tool_name"), str)}
            ),
        }
    cleanup = [
        {
            "kind": "hook_installation",
            "alias": "temporary-claude-settings",
            "action": "removed" if removed["scratch"] else "retained",
            "state": "confirmed" if removed["scratch"] else "unknown",
            "failure_attribution": "not_applicable" if removed["scratch"] else "unknown",
            "detail": (
                "Project-local .claude/settings.local.json in the scratch folder."
                if desktop
                else "Passed with --settings; user and project settings were not loaded."
            ),
            "evidence": "Scratch directory check",
        },
        {
            "kind": "temporary_state",
            "alias": "scratch-repository",
            "action": "removed" if removed["scratch"] else "retained",
            "state": "confirmed" if removed["scratch"] else "unknown",
            "failure_attribution": "not_applicable" if removed["scratch"] else "unknown",
            "detail": None,
            "evidence": "Scratch directory check",
        },
    ]
    if "provider_state" in removed:
        cleanup.append(
            {
                "kind": "temporary_state",
                "alias": "claude-project-state",
                "action": "removed" if removed["provider_state"] else "retained",
                "state": "confirmed" if removed["provider_state"] else "unknown",
                "failure_attribution": "not_applicable" if removed["provider_state"] else "unknown",
                "detail": "Claude's per-directory state for the scratch repository.",
                "evidence": "Directory check",
            }
        )
    complete = all(item["state"] == "confirmed" for item in cleanup)
    delivery = {"supported": "confirmed", "unsupported": "failed"}.get(state, "incomplete")
    return {
        "format_version": "watchdog.probe-result.v1",
        "record_id": f"wd139-claude-{surface}-{case.name}-{finished_at[:10]}-attempt-1",
        "classification": "live_provider",
        "started_at": started_at,
        "finished_at": finished_at,
        "provenance": {
            "provider": {"name": "claude", "version": version, "surface": surface},
            "harness": {"name": f"claude-{surface}", "version": None if desktop else version},
            "adapter": {"name": "none", "version": None, "invocation": "provider"},
            "os": {"name": platform.system(), "version": None, "architecture": platform.machine()},
            "source": {
                "kind": "manual-observation" if desktop else "generated-workload",
                "sanitized_reference": f"wd139-{case.name}",
            },
        },
        "scope": {
            "isolation": (
                "scratch Git repository outside registered projects with project-local hooks"
                if desktop
                else "scratch Git repository outside registered projects and a --settings file"
            ),
            "expected_callback_kinds": [event for event, _matcher in case.events],
            "expected_deliveries": 1,
            "notes": "One capability per run; hook inputs and outputs stayed in scratch.",
        },
        "stages": {
            "provider_dispatch": {
                "state": "confirmed" if dispatched else "failed",
                "attempts": 1,
                "evidence": "Claude emitted a result event" if dispatched else "No result event",
                "detail": None,
            },
            "adapter": {
                "state": "not_invoked",
                "starts": 0,
                "failure_category": None,
                "evidence": "The probe calls no Watchdog adapter.",
            },
            "callbacks": {
                "expected": expected,
                "observed": observed,
                "missing": expected - observed,
                "missing_attribution": "unknown" if observed < expected else "not_applicable",
                "evidence": "Minimum expected count per event; input shapes are in capabilities.",
            },
            "retries": {
                "count": 0,
                "scopes": [],
                "reason": None,
                "outcome": "not_attempted",
                "evidence": "One run per case.",
            },
            "persistence": {
                "accepted": 0,
                "losses": [],
                "evidence": "Not applicable: the probe does not exercise Watchdog persistence.",
            },
            "end_to_end_delivery": {
                "expected": 1,
                "persisted": 1 if state == "supported" else 0,
                "read_back": 1 if state == "supported" else 0,
                "state": delivery,
                "failure_attribution": "attributed" if state == "unsupported" else "not_applicable",
                "failure_detail": notes[0] if state == "unsupported" else None,
                "evidence": (
                    "Read back from the session transcript."
                    if desktop
                    else "Read back from the stream-json output of the same run."
                ),
            },
        },
        "capabilities": {
            case.name: {
                "state": state,
                "response_shape": case.response_shape,
                "callbacks": callbacks,
                "models": evidence.models(),
            }
        },
        "cleanup": {
            "resources": cleanup,
            "complete": complete,
            "remaining_state": None if complete else "See the cleanup resources.",
        },
        "result": {
            "state": "incomplete" if state == "inconclusive" else "passed",
            "limitations": [
                *notes,
                (
                    "One Windows desktop session, the model the user selected, one scripted prompt."
                    if desktop
                    else "One Windows Claude Code CLI version, haiku main model, one prompt."
                ),
            ],
        },
    }


def require_standalone_claude() -> None:
    if os.environ.get("CLAUDECODE"):
        raise RuntimeError("run the live probe from a standalone PowerShell or cmd.exe console")


def require_claude_login(claude: str) -> None:
    result = subprocess.run(
        [claude, "auth", "status"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=hidden_creationflags(),
    )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        status = {}
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        raise RuntimeError("Claude Code is not logged in; run `claude auth login` first")


def claude_version(claude: str) -> str:
    result = subprocess.run(
        [claude, "--version"],
        capture_output=True,
        check=True,
        text=True,
        creationflags=hidden_creationflags(),
    )
    return result.stdout.strip().split()[0]


def remove_claude_state(scratch: Path) -> bool | None:
    """Remove Claude's per-directory state for *scratch*; None when none was created."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    projects = config / "projects"
    if not projects.is_dir():
        return None
    suffix = re.sub(r"[^A-Za-z0-9]", "-", scratch.name)
    found = [entry for entry in projects.iterdir() if suffix in entry.name]
    for entry in found:
        shutil.rmtree(entry, ignore_errors=True)
    if not found:
        return None
    return not any(entry.exists() for entry in found)


def run_claude_case(
    case: ClaudeCase, args: argparse.Namespace, claude: str, version: str, roots: Sequence[Path]
) -> dict[str, object]:
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(
        prefix="watchdog-wd139-claude-", ignore_cleanup_errors=True
    ) as temporary:
        scratch = Path(temporary)
        ensure_scratch_outside_roots(scratch, roots)
        work = scratch / "work"
        control = scratch / "control"
        work.mkdir()
        control.mkdir()
        log = control / "probe.ndjson"
        settings = control / "settings.json"
        settings.write_text(
            json.dumps(claude_settings(case, Path(__file__).resolve(), log)),
            encoding="utf-8",
            newline="\n",
        )
        subprocess.run(
            ["git", "init", str(work)],
            capture_output=True,
            text=True,
            check=True,
            creationflags=hidden_creationflags(),
        )
        stdout = ""
        timed_out = False
        try:
            completed = subprocess.run(
                claude_command(claude, case, settings, args.budget),
                input=claude_stdin(case),
                capture_output=True,
                text=True,
                timeout=args.timeout,
                cwd=work,
                creationflags=hidden_creationflags(),
            )
            stdout = completed.stdout
        except subprocess.TimeoutExpired as error:
            timed_out = True
            partial = error.stdout
            stdout = (
                partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial or ""
            )
        evidence = ClaudeEvidence(
            records=_read_records(log),
            stream=parse_stream(stdout),
            files=frozenset(entry.name for entry in work.iterdir() if entry.name != ".git"),
            timed_out=timed_out,
        )
    removed: dict[str, bool] = {"scratch": not scratch.exists()}
    provider_state = remove_claude_state(scratch)
    if provider_state is not None:
        removed["provider_state"] = provider_state
    return claude_record(
        case,
        evidence,
        verdict=evaluate(case, evidence),
        version=version,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        removed=removed,
    )


def run_claude_control(args: argparse.Namespace) -> list[dict[str, object]]:
    require_standalone_claude()
    roots = registered_roots(args.config)
    ensure_scratch_outside_roots(Path(tempfile.gettempdir()), roots)
    claude = _resolve_executable(args.claude, "Claude Code CLI", "--claude")
    require_claude_login(claude)
    version = claude_version(claude)
    names = sorted(CLAUDE_CASES) if args.all else args.case
    return [run_claude_case(CLAUDE_CASES[name], args, claude, version, roots) for name in names]


# --- Claude desktop kit ---------------------------------------------------------------
#
# The desktop app cannot be driven headlessly, so the user opens one scratch folder and
# pastes one `WD139[<case>] ...` prompt per new chat. A single hook handler routes each
# callback to that case (the prompt prefix selects it at UserPromptSubmit), and
# `desktop-collect` judges every case from the hook log plus the session transcript.

DESKTOP_EXCLUDED = frozenset(
    {
        "agent-rewrite",  # the transcript carries no per-model usage
        "permission-observe",
        "pre-compact-block",  # need several turns
        "pre-compact-context",
        "session-end-observe",
        "subagent-start-context",
        "subagent-stop-block",
    }
)
DESKTOP_EVENTS: tuple[tuple[str, str | None], ...] = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "Bash|Agent"),
    ("PostToolUse", "Bash"),
    ("PermissionRequest", "Bash|Agent"),
    ("SubagentStart", None),
    ("Stop", None),
)
ROUTE = re.compile(r"WD139\[([a-z0-9-]+)\]")
IDLE = "idle"
FIRST_CASE = "session-start-context"
# `echo *` is allowlisted in the desktop settings, so permission cases write with printf.
DESKTOP_WRITE_COMMAND = f"printf WD139_PERMISSION > {SENTINEL}"


def desktop_cases() -> list[ClaudeCase]:
    return [
        case
        for case in CLAUDE_CASES.values()
        if len(case.prompts) == 1 and case.name not in DESKTOP_EXCLUDED
    ]


def desktop_prompt(case: ClaudeCase) -> str:
    return f"WD139[{case.name}] " + case.prompts[0].replace(_WRITE_COMMAND, DESKTOP_WRITE_COMMAND)


def _scratch_files(directory: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name not in {".git", ".claude", ".wd139"}
    )


def handle_routed(directory: Path) -> int:
    """Answer a desktop callback with the response of the case the last prompt selected."""
    control = directory / ".wd139"
    case_file = control / "case"
    try:
        payload, event = _read_hook_input()
        prompt = payload.get("prompt")
        route = (
            ROUTE.search(prompt)
            if event == "UserPromptSubmit" and isinstance(prompt, str)
            else None
        )
        if route is not None and route.group(1) in CLAUDE_CASES:
            case_file.write_text(route.group(1), encoding="utf-8")
            (directory / SENTINEL).unlink(missing_ok=True)  # each case starts clean
        name = case_file.read_text(encoding="utf-8").strip() if case_file.is_file() else IDLE
        case = CLAUDE_CASES.get(name)
        response = case.responses.get(event, _no_response)(payload) if case is not None else {}
        record: dict[str, object] = {
            "event": event,
            "case": name,
            "input": payload,
            "output": response,
        }
        finished = event == "Stop" and not response
        if finished:
            # Snapshot the folder per case: a later case may create or remove the sentinel.
            record["files"] = _scratch_files(directory)
        _append_log(control / "probe.ndjson", record)
        # A finished turn disarms the case so the next chat's SessionStart stays quiet.
        if finished:
            case_file.write_text(IDLE, encoding="utf-8")
        print(json.dumps(response, sort_keys=True))
    except (OSError, TypeError, ValueError) as error:
        _append_log(control / "probe.ndjson", {"error": type(error).__name__})
        print("{}")
    return 0


def desktop_settings(directory: Path) -> dict[str, object]:
    command = shlex.join(
        [
            Path(sys.executable).as_posix(),
            Path(__file__).resolve().as_posix(),
            "claude-routed-handler",
            "--dir",
            directory.as_posix(),
        ]
    )
    hooks: dict[str, list[dict[str, object]]] = {}
    for event, matcher in DESKTOP_EVENTS:
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 30}]
        }
        if matcher is not None:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    # PowerShell is denied so the model uses Bash, the tool the hooks are matched to.
    return {
        "hooks": hooks,
        "permissions": {"allow": ["Bash(echo *)", "Agent"], "deny": ["PowerShell"]},
    }


def prepare_desktop(directory: Path | None, roots: Sequence[Path]) -> dict[str, object]:
    target = (
        directory
        if directory is not None
        else Path(tempfile.mkdtemp(prefix="watchdog-wd139-desktop-"))
    )
    ensure_scratch_outside_roots(target, roots)
    (target / ".claude").mkdir(parents=True, exist_ok=True)
    (target / ".wd139").mkdir(exist_ok=True)
    (target / ".claude" / "settings.local.json").write_text(
        json.dumps(desktop_settings(target), indent=2), encoding="utf-8", newline="\n"
    )
    (target / ".wd139" / "case").write_text(FIRST_CASE, encoding="utf-8")
    subprocess.run(
        ["git", "init", str(target)],
        capture_output=True,
        text=True,
        check=True,
        creationflags=hidden_creationflags(),
    )
    return {
        "dir": str(target),
        "steps": [{"case": case.name, "prompt": desktop_prompt(case)} for case in desktop_cases()],
    }


def transcript_stream(
    events: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], str | None]:
    """Reshape a session transcript into the `stream-json` events the judges read."""
    kept: list[dict[str, object]] = []
    models: set[str] = set()
    reply = ""
    version: str | None = None
    for entry in events:
        if isinstance(entry.get("version"), str):
            version = str(entry["version"])
        message = entry.get("message")
        if entry.get("type") == "assistant" and isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and not model.startswith("<"):
                models.add(model)
            text = _block_text(message.get("content"))
            if text:
                reply = text
        if entry.get("type") in ("user", "system"):
            kept.append(dict(entry))
    kept.append(
        {
            "type": "result",
            "result": reply,
            "is_error": False,
            "modelUsage": {model: {} for model in models},
        }
    )
    return kept, version


def collect_desktop(directory: Path, *, clean: bool) -> dict[str, object]:
    log = directory / ".wd139" / "probe.ndjson"
    logged = _read_records(log)
    # The settings file is written when the folder is prepared, so it marks the start.
    prepared = directory / ".claude" / "settings.local.json"
    started = datetime.fromtimestamp(
        prepared.stat().st_mtime if prepared.is_file() else log.stat().st_mtime, UTC
    )
    gathered: list[tuple[ClaudeCase, ClaudeEvidence, str | None]] = []
    not_run: list[str] = []
    for case in desktop_cases():
        tagged = [record for record in logged if record.get("case") == case.name]
        # The prompt binds a case to its chat; a later chat's SessionStart can still carry the
        # case when the turn ended without a Stop (for example after a denied dialog).
        sessions = [
            payload.get("session_id")
            for record in tagged
            if record.get("event") == "UserPromptSubmit"
            and isinstance(payload := record.get("input"), dict)
        ]
        if not sessions:
            not_run.append(case.name)
            continue
        mine = [
            record
            for record in tagged
            if isinstance(payload := record.get("input"), dict)
            and payload.get("session_id") == sessions[-1]
        ]
        stream, version = transcript_stream(parse_stream(_read_rollout(_rollout_path(mine))))
        snapshots = [
            [name for name in listed if isinstance(name, str)]
            for record in mine
            if isinstance(listed := record.get("files"), list)
        ]
        files = frozenset(snapshots[-1] if snapshots else _scratch_files(directory))
        gathered.append((case, ClaudeEvidence(records=mine, stream=stream, files=files), version))
    removed = {"scratch": False}
    if clean:
        shutil.rmtree(directory, ignore_errors=True)
        # The desktop app can keep the emptied folder open; its contents are what matter.
        removed = {"scratch": not directory.exists() or not any(directory.iterdir())}
        provider_state = remove_claude_state(directory)
        if provider_state is not None:
            removed["provider_state"] = provider_state
    finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    records = [
        claude_record(
            case,
            evidence,
            verdict=evaluate(case, evidence),
            version=version,
            started_at=started.isoformat().replace("+00:00", "Z"),
            finished_at=finished_at,
            removed=removed,
            surface="desktop",
        )
        for case, evidence, version in gathered
    ]
    return {"records": records, "not_run": not_run}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    handler = commands.add_parser("handler", help="internal Codex hook handler")
    handler.add_argument("--log", type=Path, required=True)
    probe = commands.add_parser("codex-context", help="run the isolated Codex context probe")
    probe.add_argument("--codex", default="codex")
    probe.add_argument("--config", type=Path)
    probe.add_argument("--timeout", type=float, default=90)
    claude_handler = commands.add_parser("claude-handler", help="internal Claude hook handler")
    claude_handler.add_argument("--log", type=Path, required=True)
    claude_handler.add_argument("--case", required=True, choices=sorted(CLAUDE_CASES))
    control = commands.add_parser("claude-control", help="run isolated Claude Code control probes")
    selection = control.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", action="append", choices=sorted(CLAUDE_CASES))
    selection.add_argument("--all", action="store_true")
    control.add_argument("--claude", default="claude")
    control.add_argument("--config", type=Path)
    control.add_argument("--timeout", type=float, default=180)
    control.add_argument("--budget", type=float, default=0.5, help="USD cap per case")
    routed = commands.add_parser("claude-routed-handler", help="internal Claude desktop handler")
    routed.add_argument("--dir", type=Path, required=True)
    prepare = commands.add_parser(
        "desktop-prepare", help="create the Claude desktop scratch folder"
    )
    prepare.add_argument("--dir", type=Path)
    prepare.add_argument("--config", type=Path)
    collect = commands.add_parser("desktop-collect", help="judge the desktop cases that were run")
    collect.add_argument("--dir", type=Path, required=True)
    collect.add_argument("--clean", action="store_true", help="remove the folder afterwards")
    args = parser.parse_args()
    if args.action == "handler":
        return handle(args.log)
    if args.action == "claude-handler":
        return handle_claude(args.log, args.case)
    if args.action == "claude-routed-handler":
        return handle_routed(args.dir)
    try:
        if args.action == "desktop-prepare":
            print(json.dumps(prepare_desktop(args.dir, registered_roots(args.config)), indent=2))
        elif args.action == "desktop-collect":
            print(json.dumps(collect_desktop(args.dir, clean=args.clean), indent=2, sort_keys=True))
        elif args.action == "claude-control":
            print(json.dumps(run_claude_control(args), indent=2, sort_keys=True))
        else:
            print(json.dumps(run_codex_context(args), sort_keys=True))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"result": {"state": "inconclusive", "limitations": [str(error)]}}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
