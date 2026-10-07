"""Incremental per-session state for the daemon's decision channel (WD-141).

``apply`` is a pure reducer over one stored envelope.  The state keeps counters
and hashes only: no prompt, command, or tool output text.  Hashes use the same
canonical form as ``analysis`` signatures, so a repeat seen here is a repeat there.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from agent_watchdog.analysis import _fingerprint, captured_content, tool_outcome
from agent_watchdog.facts import observed_model, turn_of

SIGNATURE_LIMIT = 16
# Tools that change files.  Codex edits through `apply_patch`.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
# A fixed list of test runners: the verification predicate is deliberately not configurable.
_TEST_RUNNER = re.compile(r"\b(pytest|cargo\s+test|npm\s+(run\s+)?test|go\s+test)\b")

StateKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Signature:
    tool: str | None
    input_hash: str | None
    output_hash: str | None
    outcome: str
    at: str


@dataclass(frozen=True, slots=True)
class SessionState:
    """One conversation agent; the coordinator has ``agent_id == ""``."""

    provider: str
    session_id: str
    agent_id: str
    turn_id: str | None = None
    turn_started_at: str | None = None
    calls: int = 0
    failures: int = 0
    failures_in_turn: int = 0
    compactions: int = 0
    compactions_in_turn: int = 0
    open_permission_prompt_since: str | None = None
    coordinator_model: str | None = None
    last_signatures: tuple[Signature, ...] = ()
    # Edit-class calls finished since the last other call finished.
    edits_since_last_call: int = 0
    # Edit-class calls finished in the current turn, and whether a test runner
    # finished after the latest of them.  Booleans and counts only: the command
    # text that proved the verification is not kept.
    edits_in_turn: int = 0
    verified_since_edit: bool = False
    last_activity: str | None = None
    ended_at: str | None = None

    @property
    def edited_without_verification(self) -> bool:
        return self.edits_in_turn > 0 and not self.verified_since_edit

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, document: str) -> "SessionState":
        data: dict[str, Any] = json.loads(document)
        data["last_signatures"] = tuple(Signature(**item) for item in data["last_signatures"])
        return cls(**data)


def initial(provider: str, session_id: str, agent_id: str) -> SessionState:
    return SessionState(provider, session_id, agent_id)


def key_of(envelope: Mapping[str, Any]) -> StateKey | None:
    """The state an envelope belongs to; None for an event without a session."""
    provider, session_id = envelope.get("provider"), envelope.get("session_id")
    if not (isinstance(provider, str) and isinstance(session_id, str) and session_id):
        return None
    agent_id = envelope.get("agent_id")
    return (provider, session_id, agent_id if isinstance(agent_id, str) else "")


def _later(current: str | None, received_at: str) -> str:
    try:
        newer = current is None or datetime.fromisoformat(received_at) > datetime.fromisoformat(
            current
        )
    except ValueError:
        newer = True
    return received_at if newer else current or received_at


def _hash(value: object) -> str | None:
    return None if value is None else _fingerprint(value)


def apply(state: SessionState, envelope: Mapping[str, Any]) -> SessionState:
    """Fold one envelope into ``state``; deterministic and free of side effects."""
    kind = envelope.get("kind")
    received_at = str(envelope.get("received_at"))
    provider_payload = (envelope.get("payload") or {}).get(state.provider)
    provider_payload = provider_payload if isinstance(provider_payload, dict) else {}
    changes: dict[str, Any] = {"last_activity": _later(state.last_activity, received_at)}

    turn_id = None if kind == "usage" else turn_of(dict(envelope))[0]
    new_turn = {
        "turn_started_at": received_at,
        "failures_in_turn": 0,
        "compactions_in_turn": 0,
        "edits_in_turn": 0,
        "verified_since_edit": False,
    }
    if turn_id is not None and turn_id != state.turn_id:
        changes |= {"turn_id": turn_id, **new_turn}
    elif kind == "turn.start":
        changes |= new_turn

    if kind in ("tool.start", "tool.finish", "turn.start", "turn.end", "session.end"):
        changes["open_permission_prompt_since"] = None
    if kind == "waiting":
        metadata = provider_payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("notification_type") == "permission_prompt":
            changes["open_permission_prompt_since"] = (
                state.open_permission_prompt_since or received_at
            )
    if kind == "session.end":
        changes["ended_at"] = received_at
    elif state.ended_at is not None and kind != "usage":
        # Later activity (a resumed session) reopens it.
        changes["ended_at"] = None
    if kind == "compaction.start":
        changes["compactions"] = state.compactions + 1
        in_turn = changes.get("compactions_in_turn", state.compactions_in_turn)
        changes["compactions_in_turn"] = in_turn + 1
    if kind == "tool.finish":
        changes |= _finished_call(state, provider_payload, received_at, changes)
    if not state.agent_id and (model := observed_model(dict(envelope))) is not None:
        changes["coordinator_model"] = model
    return replace(state, **changes)


def _finished_call(
    state: SessionState, provider_payload: Mapping[str, Any], received_at: str, pending: dict
) -> dict[str, Any]:
    content = captured_content(provider_payload)
    response = content.get("tool_response")
    tool = provider_payload.get("tool_name")
    if provider_payload.get("hook_event_name") == "PostToolUseFailure":
        outcome = "failure"
    else:
        outcome = tool_outcome(response) if response is not None else "unknown"
    signature = Signature(
        tool if isinstance(tool, str) else None,
        _hash(content.get("tool_input")),
        _hash(response),
        outcome,
        received_at,
    )
    failed = int(outcome == "failure")
    in_turn = pending.get("failures_in_turn", state.failures_in_turn)
    edited = tool in EDIT_TOOLS
    edits_in_turn = pending.get("edits_in_turn", state.edits_in_turn)
    verified = pending.get("verified_since_edit", state.verified_since_edit)
    if edited:
        edits_in_turn, verified = edits_in_turn + 1, False
    elif _may_have_run_tests(tool, content.get("tool_input")):
        verified = True
    return {
        "calls": state.calls + 1,
        "failures": state.failures + failed,
        "failures_in_turn": in_turn + failed,
        "last_signatures": (*state.last_signatures, signature)[-SIGNATURE_LIMIT:],
        "edits_since_last_call": state.edits_since_last_call + 1 if edited else 0,
        "edits_in_turn": edits_in_turn,
        "verified_since_edit": verified,
    }


def _may_have_run_tests(tool: object, tool_input: object) -> bool:
    """Whether a finished shell call ran, or may have run, a test suite.

    A shell call whose command was not captured counts: unknown is not "no
    tests ran", and a rule must not act on a claim the state cannot support.
    """
    if tool != "Bash":
        return False
    if not isinstance(tool_input, Mapping) or not isinstance(tool_input.get("command"), str):
        return True
    return _TEST_RUNNER.search(tool_input["command"]) is not None


def trailing_repeats(state: SessionState) -> int:
    """How many of the latest calls are identical in tool, input, output and outcome.

    Zero when the newest call has an unknown hash: an unknown repeat is not one.
    """
    signatures = state.last_signatures
    if not signatures:
        return 0
    newest = signatures[-1]
    if newest.input_hash is None or newest.output_hash is None:
        return 0
    run = 0
    for item in reversed(signatures):
        if (item.tool, item.input_hash, item.output_hash, item.outcome) != (
            newest.tool,
            newest.input_hash,
            newest.output_hash,
            newest.outcome,
        ):
            break
        run += 1
    return run


def fold(envelopes: Iterable[Mapping[str, Any]]) -> dict[StateKey, SessionState]:
    """Replay envelopes in order; the migration and tests share this with ``Store``."""
    states: dict[StateKey, SessionState] = {}
    for envelope in envelopes:
        key = key_of(envelope)
        if key is not None:
            states[key] = apply(states.get(key) or initial(*key), envelope)
    return states
