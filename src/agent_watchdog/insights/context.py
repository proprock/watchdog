"""`insights context`: how each session's context grew, reset, and compacted."""

import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import budget, timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate
from agent_watchdog.storage import persisted_envelope

MODE = "context"
TURN_LIMIT = 40
LARGEST_STEPS = 5
PROMPT_EXCERPT = 600
SUMMARY_EXCERPT = 2000
PEAK_BANDS = (150_000, 400_000, 800_000)

PROMPT = """\
Mode: context. Each item is one root session (subagents are summarized inside it). \
Occupancy is the measured number of input tokens one model request read: \
"baseline_tokens" is the first request (system prompt, tool schemas, CLAUDE.md or \
AGENTS.md, memory, and the first prompt), "peak_tokens" the largest. "turns" lists the \
user prompts with the occupancy of the first request after each, so a long session \
that keeps growing across unrelated prompts is visible. "compactions" gives each \
compaction's trigger and occupancy before and after, and the compact summary when the \
provider sent one; "resets" are drops without a compaction event (clear, resume). \
"largest_steps" are the biggest measured jumps between consecutive requests with the \
tool calls that finished in between (attribution by time, so inferred). \
"facts.context_windows_reported" lists windows the Claude CLI itself reported for some \
model ids, and "peak_share_of_window" uses them; for any other model the window is \
unknown. Claude Code compacts automatically only close to the window limit by default, \
so a session can grow far without any compaction event.

Recommend, where the evidence supports it:
- compact_instructions: text telling compaction what to keep and drop, for a \
"Compact instructions" section in CLAUDE.md, `/compact <instructions>`, or Codex \
`/compact`; base it on what these sessions actually carried (tasks, files, decisions).
- compaction_threshold: compact earlier or later. Claude Code: the \
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE environment variable (percent of the window) or the \
`--autocompact <auto|tokens>` flag; Codex: `model_auto_compact_token_limit` in \
~/.codex/config.toml.
- clear_between_tasks: where prompts switch to an unrelated task inside one growing \
session, starting fresh (`/clear` in Claude Code, a new session in Codex) is cheaper \
than carrying the old context.
- startup_context: a high baseline comes from tool schemas, MCP servers, skills and \
instruction files loaded on every request; suggest trimming them.
- session_structure or other.
Put any text or configuration to paste in "draft". Say what effect you expect \
("expected_effect") in terms of the measured numbers. Only name settings listed here \
unless you mark them as needing verification.
"""


class ContextRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal[
        "compact_instructions",
        "compaction_threshold",
        "clear_between_tasks",
        "startup_context",
        "session_structure",
        "other",
    ]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    expected_effect: str
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[ContextRecommendation]
    rule_candidates: list[RuleCandidate]


def events(
    db: sqlite3.Connection,
    kinds: tuple[str, ...],
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Envelope]:
    where = f"kind IN ({', '.join('?' for _ in kinds)})"
    params: list[object] = list(kinds)
    if provider is not None:
        where += " AND json_extract(envelope, '$.provider') = ?"
        params.append(provider)
    if session_id is not None:
        where += " AND session_id = ?"
        params.append(session_id)
    result = []
    for (document,) in db.execute(
        f"SELECT envelope FROM events WHERE {where} ORDER BY received_at", params
    ):
        event = persisted_envelope(document)
        if since is not None and event.received_at < since:
            continue
        if until is not None and event.received_at >= until:
            continue
        result.append(event)
    return result


def metadata(event: Envelope) -> dict[str, Any]:
    payload = event.payload.get(event.provider)
    value = payload.get("metadata") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def prompt(event: Envelope) -> Any:
    payload = event.payload.get(event.provider)
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    return content.get("prompt") if isinstance(content, dict) else None


def _at_or_after(sequence: list[timeline.Request], at_us: int) -> timeline.Request | None:
    return next((request for request in sequence if request.at_us >= at_us), None)


def _before(sequence: list[timeline.Request], at_us: int) -> timeline.Request | None:
    earlier = [request for request in sequence if request.at_us <= at_us]
    return earlier[-1] if earlier else None


def _turns(sequence: list[timeline.Request], starts: list[Envelope]) -> list[dict[str, Any]]:
    turns = []
    for event in starts:
        after = _at_or_after(sequence, timeline.us(event.received_at) or 0)
        turns.append(
            {
                "evidence_id": str(event.event_id),
                "at": event.received_at.isoformat(),
                "occupancy_after": after.occupancy if after else None,
                "prompt": excerpt(prompt(event), PROMPT_EXCERPT),
            }
        )
    if len(turns) > TURN_LIMIT:
        # Keep how the session began and how it ended; the count is still reported.
        turns = turns[:10] + turns[-(TURN_LIMIT - 10) :]
    return turns


def _compactions(sequence: list[timeline.Request], events_: list[Envelope]) -> list[dict[str, Any]]:
    result = []
    starts = [event for event in events_ if event.kind == "compaction.start"]
    ends = [event for event in events_ if event.kind == "compaction.end"]
    for start in starts:
        at = timeline.us(start.received_at) or 0
        end = next((event for event in ends if event.received_at >= start.received_at), None)
        summary = metadata(end).get("compact_summary") if end else None
        before = _before(sequence, at)
        after = _at_or_after(sequence, timeline.us(end.received_at) or at) if end else None
        result.append(
            {
                "evidence_id": str(start.event_id),
                "at": start.received_at.isoformat(),
                "trigger": metadata(start).get("trigger"),
                "before_tokens": before.occupancy if before else None,
                "after_tokens": after.occupancy if after else None,
                "summary_chars": len(summary) if isinstance(summary, str) else None,
                "summary": excerpt(summary, SUMMARY_EXCERPT) if isinstance(summary, str) else None,
            }
        )
    return result


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
    with inspection.database(paths, project) as db:
        series, counts = timeline.requests(db, **options)
        lifecycle = events(
            db,
            ("session.start", "turn.start", "compaction.start", "compaction.end"),
            **options,
        )
    _types, finishes = inspection.tool_finishes(paths, project, **options)
    steps, _unattributed = timeline.steps(series, finishes)

    by_session: dict[tuple[str, str | None], list[Envelope]] = defaultdict(list)
    for event in lifecycle:
        by_session[(event.provider, event.session_id)].append(event)
    steps_by_key: dict[timeline.Key, list[timeline.Step]] = defaultdict(list)
    for step in steps:
        steps_by_key[step.key].append(step)

    windows = budget.reported_windows(paths)
    profiles = []
    summaries = 0
    for key, sequence in series.items():
        provider_name, session, agent = key
        if agent is not None:
            continue
        session_events = by_session.get((provider_name, session), [])
        subagents = [
            requests
            for (other_provider, other_session, other_agent), requests in series.items()
            if other_provider == provider_name and other_session == session and other_agent
        ]
        session_steps = steps_by_key.get(key, [])
        largest = sorted(session_steps, key=lambda step: step.appended, reverse=True)
        compactions = _compactions(sequence, session_events)
        summaries += sum(1 for item in compactions if item["summary_chars"] is not None)
        compacted_at = [
            timeline.us(event.received_at) or 0
            for event in session_events
            if event.kind in ("compaction.start", "compaction.end")
        ]
        # A drop with a compaction event in its interval is already in `compactions`.
        resets = [
            {
                "evidence_id": step.after.event_id,
                "at_us": step.after.at_us,
                "before_tokens": step.before.occupancy,
                "after_tokens": step.after.occupancy,
            }
            for step in session_steps
            if step.reset
            and not any(step.before.at_us <= at <= step.after.at_us for at in compacted_at)
        ]
        sources = [
            metadata(event).get("source")
            for event in session_events
            if event.kind == "session.start"
        ]
        starts = [event for event in session_events if event.kind == "turn.start"]
        peak = max(request.occupancy for request in sequence)
        profiles.append(
            {
                "provider": provider_name,
                "session_id": session,
                "models": sorted({request.model for request in sequence if request.model}),
                "start_sources": sorted({source for source in sources if source}),
                "requests": len(sequence),
                "first_at_us": sequence[0].at_us,
                "last_at_us": sequence[-1].at_us,
                "baseline_tokens": sequence[0].occupancy,
                "peak_tokens": peak,
                "peak_share_of_window": _share(peak, sequence, windows),
                "final_tokens": sequence[-1].occupancy,
                "output_tokens": sum(request.output or 0 for request in sequence),
                "user_prompts": len(starts),
                "turns": _turns(sequence, starts),
                "compactions": compactions,
                "resets_count": len(resets),
                "resets": resets[:10],
                "largest_steps": [
                    {
                        "evidence_id": step.after.event_id,
                        "appended_tokens": step.appended,
                        "tools": [
                            {
                                "evidence_id": str(event.event_id),
                                "tool_name": timeline.tool_name(event),
                                "input": excerpt(timeline.tool_content(event)[0], 400),
                            }
                            for event in step.finishes[:5]
                        ],
                    }
                    for step in largest[:LARGEST_STEPS]
                    if step.appended > 0
                ],
                "subagents": {
                    "count": len(subagents),
                    "requests": sum(len(requests) for requests in subagents),
                    "peak_tokens": max(
                        (request.occupancy for requests in subagents for request in requests),
                        default=None,
                    ),
                },
            }
        )

    profiles.sort(key=lambda profile: (profile["peak_tokens"], profile["requests"]), reverse=True)
    items = [{"item_id": f"C{index}", **profile} for index, profile in enumerate(profiles, 1)]
    facts = {
        "sessions": len(profiles),
        "context_windows_reported": windows,
        "by_provider": _provider_facts(profiles),
    }
    coverage = counts | {
        "sessions_without_usage": len(
            {
                (event.provider, event.session_id)
                for event in lifecycle
                if event.kind == "turn.start"
            }
            - {(profile["provider"], profile["session_id"]) for profile in profiles}
        ),
        "compaction_summaries": summaries,
        "notes": [
            "Occupancy: Claude input + cache read + cache write; Codex input_tokens, of which "
            "cached tokens are a subset.",
            "Step attribution to tool calls is inferred from time order; a step also carries "
            "prompts and provider reminders.",
            "Compact summaries expire with retained content; Claude PreCompact custom "
            "instructions have never been observed.",
            "Context windows are known only for model ids the Claude CLI reported during an "
            "insights call; other windows are unknown.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)


def _share(peak: int, sequence: list[timeline.Request], windows: dict[str, int]) -> float | None:
    """Peak as a share of the window, only when every model in the session has a known one."""
    models = {request.model for request in sequence if request.model}
    known = [windows[model] for model in models if model in windows]
    if not known or len(known) != len(models):
        return None
    return round(peak / min(known), 3)


def _provider_facts(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for profile in profiles:
        grouped[profile["provider"]].append(profile)
    result = {}
    for name, members in sorted(grouped.items()):
        baselines = [member["baseline_tokens"] for member in members]
        peaks = [member["peak_tokens"] for member in members]
        compactions = [compaction for member in members for compaction in member["compactions"]]
        triggers: dict[str, int] = defaultdict(int)
        for compaction in compactions:
            triggers[str(compaction["trigger"])] += 1
        result[name] = {
            "sessions": len(members),
            "baseline_tokens_median": round(median(baselines)),
            "peak_tokens_median": round(median(peaks)),
            "peak_tokens_max": max(peaks),
            "sessions_peaking_above": {
                str(band): sum(1 for peak in peaks if peak > band) for band in PEAK_BANDS
            },
            "compactions": dict(triggers),
            "resets_without_compaction": sum(member["resets_count"] for member in members),
        }
    return result
