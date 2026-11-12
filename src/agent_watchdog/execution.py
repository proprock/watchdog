"""Versioned, read-only Codex execution projection (WD-164).

Native observations remain authoritative. Only native call and execution IDs
join observations; command text and proximity are never correlation keys.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agent_watchdog.events import Envelope

VERSION = "wd-164.v1"
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "exec_command", "shell", "shell_command"})
_EXIT = re.compile(r"Process exited with code (-?\d+)\Z")
_RUNNING = re.compile(r"Process running with session ID ([\w-]+)\Z")


@dataclass(frozen=True)
class ResultFacts:
    phase: str = "unknown"
    outcome: str = "unknown"
    exit_code: int | None = None
    session_id: str | None = None
    provenance: str = "missing_result"


def _identity(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return str(value) if type(value) is int else None


def tool_name(value: object) -> str | None:
    return value.rsplit(".", 1)[-1] if isinstance(value, str) and value else None


def _input(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def command_input(tool: object, value: object) -> object:
    """Canonical shell input, retaining execution arguments besides the alias."""
    value = _input(value)
    if tool_name(tool) not in SHELL_TOOLS or not isinstance(value, Mapping):
        return value
    result = dict(value)
    command = result.pop("cmd", None)
    if "command" not in result and command is not None:
        result["command"] = command
    # These affect delivery, not the command that was executed.
    result.pop("yield_time_ms", None)
    result.pop("max_output_tokens", None)
    return result


def _text_result(text: str) -> ResultFacts:
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, (dict, list)):
        return result_facts(value)
    # Only the provider's leading result frame is evidence. Text after Output
    # belongs to the command and must not supply an exit code or process ID.
    for line in text.splitlines():
        line = line.strip()
        if line in {"Output:", "Final output:"}:
            break
        match = _EXIT.fullmatch(line)
        if match:
            code = int(match.group(1))
            return ResultFacts(
                "completed",
                "success" if code == 0 else "failure",
                code,
                provenance="provider_result_frame",
            )
        match = _RUNNING.fullmatch(line)
        if match:
            return ResultFacts(
                "running", session_id=match.group(1), provenance="provider_result_frame"
            )
        if line.startswith("Script running with cell ID "):
            return ResultFacts("running", provenance="code_mode_frame")
        if line == "Script completed":
            return ResultFacts("completed", provenance="code_mode_frame")
        if not line.startswith(("Chunk ID:", "Wall time:", "Original token count:")):
            break
    return ResultFacts(provenance="unclassified_result")


def result_facts(value: object) -> ResultFacts:
    """Read structured result evidence; absence and wrapper completion are unknown."""
    if isinstance(value, str):
        return _text_result(value)
    if isinstance(value, list):
        # A code-mode frame describes only its script, even when later content
        # blocks print a successful child result with no child identity.
        for block in value:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                frame = _text_result(block["text"])
                if frame.provenance == "code_mode_frame":
                    return frame
        known = [
            result_facts(block.get("text"))
            for block in value
            if isinstance(block, Mapping) and block.get("type") in {"text", "output_text"}
        ]
        return (
            _consistent(known) if known else ResultFacts(provenance="unclassified_content_blocks")
        )
    if not isinstance(value, Mapping):
        return ResultFacts()
    if any(
        value.get(key) is True for key in ("interrupted", "cancelled", "canceled", "is_interrupt")
    ):
        return ResultFacts("interrupted", "interrupt", provenance="structured_interrupt")
    if value.get("status") in ("interrupted", "cancelled", "canceled"):
        return ResultFacts("interrupted", "interrupt", provenance="structured_status")
    for key in ("exit_code", "exitCode"):
        code = value.get(key)
        if type(code) is int:
            return ResultFacts(
                "completed",
                "success" if code == 0 else "failure",
                code,
                provenance=f"structured_{key}",
            )
    if value.get("isError") is True or value.get("is_error") is True:
        return ResultFacts("completed", "failure", provenance="structured_error")
    if value.get("status") == "failed":
        return ResultFacts("completed", "failure", provenance="structured_status")
    process = _identity(value.get("session_id"))
    if process is not None:
        return ResultFacts("running", session_id=process, provenance="structured_session_id")
    if value.get("status") == "completed":
        return ResultFacts("completed", provenance="structured_status_without_verdict")
    return ResultFacts(provenance="unclassified_structured_result")


def _consistent(results: list[ResultFacts]) -> ResultFacts:
    terminal = [r for r in results if r.outcome != "unknown"]
    if (
        len({r.outcome for r in terminal}) > 1
        or len({r.exit_code for r in terminal if r.exit_code is not None}) > 1
    ):
        return ResultFacts(provenance="conflicting_results")
    if terminal:
        return next((r for r in terminal if r.exit_code is not None), terminal[0])
    return next(
        (r for r in results if r.phase == "completed"),
        next((r for r in results if r.phase != "unknown"), ResultFacts()),
    )


def _payload(event: Envelope) -> dict[str, Any]:
    value = event.payload.get(event.provider)
    return value if isinstance(value, dict) else {}


def _content(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("content")
    return value if isinstance(value, Mapping) else {} if "content" in payload else payload


def _metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _call_id(event: Envelope) -> str | None:
    payload = _payload(event)
    return (
        _identity(payload.get("tool_use_id"))
        or _identity(payload.get("call_id"))
        or _identity(_metadata(payload).get("call_id"))
    )


def _scope(event: Envelope) -> tuple[str, str | None, str | None]:
    return str(event.project_id), event.session_id, event.agent_id


def tool_result(payload: Mapping[str, Any], tool: str | None = None) -> ResultFacts:
    """Shared single-call classifier, including native patch and code-mode results."""
    tool = tool or tool_name(payload.get("tool_name"))
    metadata = _metadata(payload)
    if metadata.get("is_interrupt") is True:
        return ResultFacts("interrupted", "interrupt", provenance="provider_interrupt")
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return ResultFacts("completed", "failure", provenance="provider_failure_hook")
    response = _content(payload).get("tool_response")
    facts = result_facts(response)
    if tool == "exec" and isinstance(_content(payload).get("tool_input"), str):
        return replace(
            facts,
            outcome="unknown",
            exit_code=None,
            provenance="code_mode_frame"
            if facts.provenance == "code_mode_frame"
            else "nested_identity_unavailable",
        )
    if tool == "apply_patch" and isinstance(response, Mapping):
        if type(response.get("success")) is bool:
            return ResultFacts(
                "completed",
                "success" if response["success"] else "failure",
                provenance="structured_patch_success",
            )
        if (
            response.get("status") == "completed"
            and isinstance(response.get("stdout"), str)
            and response["stdout"].startswith("Success.")
        ):
            return ResultFacts("completed", "success", provenance="structured_patch_status")
    if (
        tool == "apply_patch"
        and isinstance(response, str)
        and response.startswith("Success. Updated the following files:\n")
    ):
        return ResultFacts("completed", "success", provenance="patch_result_frame")
    return facts


def _result(event: Envelope, tool: str | None) -> ResultFacts:
    return tool_result(_payload(event), tool)


@dataclass
class _Call:
    rows: list[Envelope] = field(default_factory=list)
    tool: str | None = None
    input: object = None
    finishes: list[Envelope] = field(default_factory=list)
    results: list[ResultFacts] = field(default_factory=list)
    polls: list[_Call] = field(default_factory=list)

    def add(self, event: Envelope) -> None:
        if event in self.rows:
            return
        self.rows.append(event)
        payload = _payload(event)
        tool = tool_name(payload.get("tool_name"))
        if self.tool is None or self.tool in {"exec_command", "shell", "shell_command"}:
            self.tool = tool or self.tool
        value = _content(payload).get("tool_input")
        if self.input is None and value is not None:
            self.input = _input(value)
        if event.kind == "tool.finish":
            self.finishes.append(event)

    def prepare(self) -> None:
        self.results = [_result(e, self.tool) for e in self.finishes]


def _effective_time(event: Envelope):
    return (
        event.occurred_at
        if event.source == "transcript" and event.occurred_at is not None
        else event.received_at
    )


def _derived(
    event: Envelope,
    call: _Call,
    result: ResultFacts,
    evidence: list[Envelope],
    *,
    kind: str | None = None,
) -> Envelope:
    payload = dict(_payload(event))
    content = dict(_content(payload))
    canonical = (
        "Bash"
        if call.tool in {"exec_command", "shell", "shell_command"}
        else "Edit"
        if call.tool == "apply_patch"
        else call.tool
    )
    tool_input = command_input(call.tool, call.input)
    content.pop("tool_input", None)
    if tool_input is not None:
        content["tool_input"] = tool_input
    # Choose the response that supplied the verdict, never a yield response
    # from the command's first hook when completion arrived through a poll.
    candidates = [row for row in evidence if row.kind == "tool.finish"]
    chosen = next((row for row in reversed(candidates) if _result(row, call.tool) == result), None)
    if chosen is not None:
        response = _content(_payload(chosen)).get("tool_response")
        if response is not None:
            content["tool_response"] = response
    if result.exit_code is not None:
        response = content.get("tool_response")
        if not isinstance(response, Mapping):
            content["tool_response"] = {"exit_code": result.exit_code, "output": response}
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    payload.update(
        {
            "tool_name": canonical,
            "tool_use_id": _call_id(call.rows[0]),
            "content": content,
            "execution": {
                "version": VERSION,
                "phase": result.phase,
                "outcome": result.outcome,
                "command": command if isinstance(command, (str, list)) else None,
                "exit_code": result.exit_code,
                "session_id": result.session_id,
                "provenance": result.provenance,
                "evidence_ids": list(dict.fromkeys(str(row.event_id) for row in evidence)),
            },
        }
    )
    return event.model_copy(
        update={
            "payload": {event.provider: payload},
            "kind": kind or event.kind,
            "received_at": _effective_time(event),
            "availability": {
                **event.availability,
                "tool_outcome": "observed" if result.outcome != "unknown" else "unknown",
            },
        }
    )


def normalize_events(events: Iterable[Envelope]) -> list[Envelope]:
    """Project completed executions once, including late hook/rollout enrichment.

    The original hook finish anchors duplicate evidence. Yielded commands finish
    at the terminal poll, using their original input. Polls lacking a native
    execution match remain waiting observations, never completed commands.
    """
    ordered = list(events)
    if not any(
        e.provider == "codex"
        and e.kind in {"tool.start", "tool.finish"}
        and not isinstance(_payload(e).get("execution"), Mapping)
        for e in ordered
    ):
        return ordered
    calls: dict[tuple, _Call] = {}
    other = []
    for event in ordered:
        if event.provider != "codex" or event.kind not in {"tool.start", "tool.finish"}:
            other.append(event)
            continue
        native = _call_id(event) if event.session_id is not None else None
        key = (*_scope(event), native or str(event.event_id))
        calls.setdefault(key, _Call()).add(event)
    processes: dict[tuple, list[_Call]] = {}
    polls = []
    for call in calls.values():
        call.prepare()
        if call.tool == "write_stdin":
            polls.append(call)
            continue
        for result in call.results:
            if result.phase == "running" and result.session_id is not None:
                key = (*_scope(call.rows[0]), result.session_id)
                if call not in processes.setdefault(key, []):
                    processes[key].append(call)
    linked = set()
    for poll in polls:
        data = poll.input
        process = _identity(data.get("session_id")) if isinstance(data, Mapping) else None
        matches = processes.get((*_scope(poll.rows[0]), process), [])
        if len(matches) == 1 and poll.rows[0].session_id is not None:
            matches[0].polls.append(poll)
            linked.add(id(poll))
    for call in calls.values():
        if id(call) in linked:
            continue
        # Keep starts for duration/context consumers, while deriving their
        # canonical input from all available evidence for the native call.
        hook_times = [_effective_time(row) for row in call.rows if row.source == "hook"]
        for row in call.rows:
            if row.kind == "tool.start":
                start = _derived(row, call, ResultFacts(), call.rows)
                if hook_times:
                    start = start.model_copy(
                        update={"received_at": min(start.received_at, *hook_times)}
                    )
                other.append(start)
        evidence = list(call.rows)
        results = list(call.results)
        terminals = list(call.finishes)
        for poll in sorted(call.polls, key=lambda c: _effective_time(c.rows[0])):
            evidence.extend(poll.rows)
            results.extend(poll.results)
            terminals.extend(poll.finishes)
        if not terminals:
            continue
        result = _consistent(results)
        if call.tool == "write_stdin":
            result = replace(
                result, phase="unknown", outcome="unknown", provenance="unmatched_execution_poll"
            )
        known = [
            row for row in terminals if _result(row, call.tool).phase not in {"running", "unknown"}
        ]
        anchors = known or call.finishes
        if not call.polls:
            anchors = [
                row for row in call.finishes if _result(row, call.tool).phase != "running"
            ] or anchors
        hooks = [row for row in anchors if row.source == "hook"]
        anchor = min(hooks or anchors, key=_effective_time)
        kind = (
            "waiting" if result.phase == "running" or call.tool == "write_stdin" else "tool.finish"
        )
        other.append(_derived(anchor, call, result, evidence, kind=kind))
    return sorted(other, key=lambda e: e.received_at)
