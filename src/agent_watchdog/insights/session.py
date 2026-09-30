"""`insights session`: a second opinion on whether one session is making progress (WD-014)."""

from collections import Counter
from datetime import datetime
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate
from agent_watchdog.insights.tokens import command_class

MODE = "session"
REQUIRES_SESSION = True
CALLS_PER_TURN = 120
# A long turn keeps its opening calls and its most recent ones, where a loop shows.
CALLS_HEAD = 20
FINDING_EVIDENCE = 20
LIFECYCLE = (
    "session.start",
    "session.end",
    "turn.start",
    "turn.end",
    "compaction.start",
    "compaction.end",
    "agent.start",
    "waiting",
)

PROMPT = """\
Mode: session. The bundle describes one coding session. "facts" holds the task as \
first stated ("first_prompt"), the latest prompt and final assistant message, totals \
(turns, tool calls, failures, subagents, compactions, permission prompts, measured \
tokens), Watchdog's deterministic shadow findings with their evidence, gaps, and any \
label a person recorded. Items are the session's turns, newest first: each has the \
user's prompt, the context size after it, the tool calls in order with their class, \
outcome, duration, and an input excerpt (errors included), and the assistant's last \
message in that turn.

Judge the session as a whole, as a second opinion, not a verdict:
- progress: the work moves toward the stated task; new evidence keeps appearing.
- stuck: the agent repeats itself without new evidence: the same failure, the same \
edit and revert, re-reading without acting, or turns that end without advancing.
- blocked: the work waits on something outside the agent: a user decision, a \
permission, credentials, or a broken environment it cannot fix.
- uncertain: the evidence cannot tell, for example missing content or a session that \
just started.
Tool success is not progress, and a Stop or a final message is not task success. A \
shadow finding is a hint, not proof; say where you agree or disagree with one. Give \
the rationale with evidence ids, and in "unblock" the most useful next step, or null.

Recommendations are optional: next_step for this session, split_task when the \
session mixes unrelated work, instruction for CLAUDE.md or AGENTS.md, ask_user when \
the agent should have asked, tooling, or other.
"""


class Judgement(OutputModel):
    state: Literal["progress", "uncertain", "stuck", "blocked"]
    rationale: str
    unblock: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class SessionRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal["next_step", "split_task", "instruction", "ask_user", "tooling", "other"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    judgement: Judgement
    recommendations: list[SessionRecommendation]
    rule_candidates: list[RuleCandidate]


def _call(event: Envelope, agent_types: dict[str, str]) -> dict[str, Any]:
    status = inspection.finish_status(event)
    tool_input = timeline.tool_content(event)[0]
    row: dict[str, Any] = {
        "evidence_id": str(event.event_id),
        "class": command_class(timeline.tool_name(event), tool_input),
        "outcome": "ok"
        if timeline.succeeded(event, status)
        else {"failure": "failed", "interrupt": "interrupted"}.get(status, "unknown"),
        "duration_ms": timeline.duration_ms(event),
        "input": excerpt(tool_input, 200),
    }
    if event.agent_id is not None:
        row["agent"] = agent_types.get(event.agent_id, event.agent_id)
    if status == "failure":
        row["error"] = excerpt(inspection.error_record(event, agent_types)["error"], 400)
    return row


def _kept(calls: list[Envelope]) -> list[Envelope]:
    if len(calls) <= CALLS_PER_TURN:
        return calls
    return calls[:CALLS_HEAD] + calls[-(CALLS_PER_TURN - CALLS_HEAD) :]


def _turns(
    starts: list[Envelope],
    ends: list[Envelope],
    finishes: list[Envelope],
    requests: list[timeline.Request],
    agent_types: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Split the session at each prompt; calls before the first prompt form turn 0."""
    bounds = [start.received_at for start in starts]
    turns: list[dict[str, Any]] = []
    for index in range(len(starts) + 1):
        low = bounds[index - 1] if index else None
        high = bounds[index] if index < len(bounds) else None

        def inside(event: Envelope, low=low, high=high) -> bool:
            return (low is None or event.received_at >= low) and (
                high is None or event.received_at < high
            )

        calls = [event for event in finishes if inside(event)]
        if index == 0 and not calls:
            continue
        start = starts[index - 1] if index else None
        final = [end for end in ends if inside(end) and end.agent_id is None]
        after = (
            next((r for r in requests if r.at_us >= (timeline.us(start.received_at) or 0)), None)
            if start
            else None
        )
        failures = sum(1 for event in calls if inspection.finish_status(event) == "failure")
        turns.append(
            {
                "item_id": f"T{index}",
                "evidence_id": str(start.event_id) if start else None,
                "at": (start.received_at if start else calls[0].received_at).isoformat(),
                "prompt": excerpt(timeline.content(start, "prompt"), 1500) if start else None,
                "occupancy_after": after.occupancy if after else None,
                "calls_total": len(calls),
                "failures": failures,
                "calls": [_call(event, agent_types) for event in _kept(calls)],
                "assistant_final": excerpt(
                    timeline.content(final[-1], "last_assistant_message"), 1500
                )
                if final
                else None,
                "ended": bool(final),
            }
        )
    omitted = sum(max(0, turn["calls_total"] - CALLS_PER_TURN) for turn in turns)
    return turns, omitted


def build(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Draft:
    options = {"provider": provider, "session_id": session_id, "since": since, "until": until}
    report = inspection.report(paths, project, session_id=session_id, provider=provider or "")
    with inspection.database(paths, project) as db:
        series, counts = timeline.requests(db, **options)
        lifecycle = timeline.events(db, LIFECYCLE, **options)
    agent_types, finishes = inspection.tool_finishes(paths, project, **options)

    main = series.get((provider or "", session_id, None), [])
    starts = [event for event in lifecycle if event.kind == "turn.start"]
    ends = [event for event in lifecycle if event.kind == "turn.end"]
    turns, omitted = _turns(starts, ends, finishes, main, agent_types)
    kinds = Counter(event.kind for event in lifecycle)
    prompts = sum(
        1
        for event in lifecycle
        if event.kind == "waiting"
        and timeline.metadata(event).get("notification_type") == "permission_prompt"
    )
    subagents = Counter(
        timeline.metadata(event).get("agent_type") or "(unnamed)"
        for event in lifecycle
        if event.kind == "agent.start"
    )
    last_end = next((end for end in reversed(ends) if end.agent_id is None), None)
    facts = {
        "provider": provider,
        "session_id": session_id,
        "first_at": report["timeline"]["first_received_at"],
        "last_at": report["timeline"]["last_received_at"],
        "wall_seconds": report["timeline"]["wall_seconds"],
        "active_seconds": report["timeline"]["active_seconds"],
        "session_ended": kinds["session.end"] > 0,
        "label": report.get("label"),
        "first_prompt": excerpt(timeline.content(starts[0], "prompt"), 3000) if starts else None,
        "last_prompt": excerpt(timeline.content(starts[-1], "prompt"), 1500)
        if len(starts) > 1
        else None,
        "last_assistant_message": excerpt(
            timeline.content(last_end, "last_assistant_message"), 3000
        )
        if last_end
        else None,
        "turns": len(starts),
        "tool_calls": len(finishes),
        "tool_failures": sum(
            1 for event in finishes if inspection.finish_status(event) == "failure"
        ),
        "subagents": dict(subagents),
        "compactions": kinds["compaction.start"],
        "permission_prompts": prompts,
        "requests": len(main),
        "peak_tokens": max((request.occupancy for request in main), default=None),
        "final_tokens": main[-1].occupancy if main else None,
        "input_read_tokens": sum(request.occupancy for request in main),
        "output_tokens": sum(request.output or 0 for request in main),
        "findings": [
            {
                "rule": finding["rule"],
                "rule_version": finding["rule_version"],
                "count": finding["count"],
                "attribution": finding["attribution"],
                "explanation": finding["explanation"],
                "evidence_ids": finding["evidence_ids"][:FINDING_EVIDENCE],
            }
            for finding in report["findings"]
        ],
        "gaps": report["gaps"],
    }
    coverage = counts | {
        "calls_omitted_from_turns": omitted,
        "notes": [
            "Items are turns, newest first, so a budget cut drops the oldest turns; the "
            "first prompt stays in facts.",
            f"Each turn lists at most {CALLS_PER_TURN} calls, its first {CALLS_HEAD} and "
            "its latest; the rest are counted.",
            "Content that expired or was never captured leaves prompts, inputs and messages "
            "empty; that is missing evidence, not silence.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=list(reversed(turns)))
