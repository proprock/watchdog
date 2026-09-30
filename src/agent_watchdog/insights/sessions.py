"""`insights sessions`: triage of a project's sessions in a window (WD-133)."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal

from agent_watchdog import analysis, inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline, workflow
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate

MODE = "sessions"
# The mode reads every session of one project; a single session has `insights session`.
PROJECT_WIDE = True
# A failing run this long is trouble, not noise: the same threshold as a repetition.
FAILURE_SIGNAL = 3
PROMPT_EXCERPT = 600
MESSAGE_EXCERPT = 600
NOTE_EXCERPT = 300
FINDING_EVIDENCE = 3
FAILURE_EVIDENCE = 3
SIGNALS = {
    "findings": "at least one shadow finding (repeated outcome, identical error, "
    "repeated test failure, diff oscillation)",
    "loops": f"a run of at least {workflow.LOOP_MIN} back-to-back calls of one class "
    "that mostly repeats the same input",
    "failures": f"at least {FAILURE_SIGNAL} failed tool calls",
    "no_end": "no session end was observed (the session may still be running); it "
    "ranks after the other signals and does not count as trouble by itself",
}
# Without an observed turn boundary a diff oscillation cannot be tied to one session
# (`analysis.analyze` keeps it for every session on the checkout), so it stays unknown.
UNATTRIBUTED = "unattributed_oscillations"
# A snapshot is taken just after the hook event that triggers it.
SNAPSHOT_GRACE = timedelta(seconds=30)

PROMPT = """\
Mode: sessions. The bundle describes the sessions of one project in a time window. \
"facts" holds project totals and the definition of each trouble signal. Items are \
sessions, ranked by trouble with the most troubled first, so a budget cut drops the \
calmest ones ("coverage.truncated"). Each item is a compact facet: the first prompt, \
the last assistant message, turns, tool calls and failures, loops, shadow findings, \
measured tokens, compactions, subagents, whether an end was observed, any label a \
person recorded, its trouble "signals", and evidence ids.

Triage the sessions; do not judge one in depth (the user can open it with `insights \
session`):
- candidates: sessions that look stuck (the agent repeats itself without new \
evidence), blocked (waiting on something outside the agent), or abandoned (they stop \
unfinished after trouble), or uncertain when a signal is present but the evidence \
cannot tell. Give the rationale and a suggestion for the user, citing the session's \
item id and evidence ids. Prefer fewer, stronger candidates; a calm, finished session \
is not a candidate.
- patterns: recurring session-level problems across two or more sessions, such as \
unrelated tasks mixed in one session, sessions abandoned after the same kind of \
failure, or long sessions that never compact. Cite every session involved.
- Recommendations and rule_candidates are optional; recommend only what the pattern \
justifies.
No end observed is not abandonment: a session may still be running. Tool success is \
not progress, and a final message is not task success. A recorded label is a person's \
judgement; treat it as evidence, and never propose changing one.
"""


class Candidate(OutputModel):
    state: Literal["stuck", "blocked", "abandoned", "uncertain"]
    title: str
    rationale: str
    item_ids: list[str]
    suggestion: str
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Pattern(OutputModel):
    title: str
    kind: Literal["mixed_tasks", "abandoned_after_failure", "never_compacts", "other"]
    description: str
    item_ids: list[str]
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class SessionsRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal["instruction", "split_task", "ask_user", "tooling", "other"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    candidates: list[Candidate]
    patterns: list[Pattern]
    recommendations: list[SessionsRecommendation]
    rule_candidates: list[RuleCandidate]


SessionKey = tuple[str, str]


def _first_id(events: list[Envelope]) -> list[str]:
    return [str(events[0].event_id)] if events else []


def _facet(
    key: SessionKey,
    group: list[Envelope],
    *,
    findings: list[dict[str, Any]],
    unattributed: int,
    label: inspection.SessionLabel,
    requests: list[timeline.Request],
    loops: list[workflow.Block],
) -> dict[str, Any]:
    starts = [event for event in group if event.kind == "turn.start"]
    ends = [event for event in group if event.kind == "turn.end" and event.agent_id is None]
    finishes = [event for event in group if event.kind == "tool.finish"]
    failed = [event for event in finishes if inspection.finish_status(event) == "failure"]
    kinds = Counter(event.kind for event in group)
    ended = kinds["session.end"] > 0
    longest = max(loops, key=len, default=[])
    signals = [
        name
        for name, hit in (
            ("findings", bool(findings)),
            ("loops", bool(loops)),
            ("failures", len(failed) >= FAILURE_SIGNAL),
            ("no_end", not ended),
        )
        if hit
    ]
    evidence = [
        *_first_id(starts),
        *[str(event.event_id) for event in ends[-1:]],
        *[str(event.event_id) for event in failed[:FAILURE_EVIDENCE]],
        *[str(event.event_id) for event, _name in longest[:1]],
    ]
    return {
        "provider": key[0],
        "session_id": key[1],
        "first_at": group[0].received_at.isoformat(),
        "last_at": group[-1].received_at.isoformat(),
        "first_prompt": excerpt(timeline.content(starts[0], "prompt"), PROMPT_EXCERPT)
        if starts
        else None,
        "last_assistant_message": excerpt(
            timeline.content(ends[-1], "last_assistant_message"), MESSAGE_EXCERPT
        )
        if ends
        else None,
        "turns": len(starts),
        "tool_calls": len(finishes),
        "tool_failures": len(failed),
        "loops": {"count": len(loops), "longest": len(longest)},
        UNATTRIBUTED: unattributed,
        "findings": [
            {
                "rule": finding["rule"],
                "count": finding["count"],
                "evidence_ids": finding["evidence_ids"][:FINDING_EVIDENCE],
            }
            for finding in findings
        ],
        "requests": len(requests),
        "peak_tokens": max((request.occupancy for request in requests), default=None),
        "final_tokens": requests[-1].occupancy if requests else None,
        "output_tokens": sum(request.output or 0 for request in requests),
        "compactions": kinds["compaction.start"],
        "subagents": dict(
            Counter(
                timeline.metadata(event).get("agent_type") or "(unnamed)"
                for event in group
                if event.kind == "agent.start"
            )
        ),
        "ended": ended,
        "label": dict(label) | {"reviewer_note": excerpt(label["reviewer_note"], NOTE_EXCERPT)},
        "signals": signals,
        "evidence_ids": list(dict.fromkeys(evidence)),
    }


def _rank(facet: dict[str, Any]) -> tuple:
    """Most troubled first; ties go to the most recent session, then to the id."""
    last = datetime.fromisoformat(facet["last_at"]).timestamp()
    return (
        -len([name for name in facet["signals"] if name != "no_end"]),
        -len(facet["findings"]),
        -facet["loops"]["count"],
        -facet["tool_failures"],
        facet["ended"],
        -last,
        facet["provider"],
        facet["session_id"],
    )


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
        events = timeline.events(db, None, **options)
        checkouts = {str(event.checkout_id) for event in events if event.checkout_id is not None}
        has_snapshots = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diff_snapshots'"
        ).fetchone()
        snapshots = inspection.snapshots_for_checkouts(db, checkouts) if has_snapshots else []
        groups: dict[SessionKey, list[Envelope]] = defaultdict(list)
        for event in events:
            if event.session_id is not None:
                groups[(event.provider, event.session_id)].append(event)
        labels = {key: inspection.session_label(db, *key) for key in groups}
    loops = workflow.loops_by_session([event for event in events if event.kind == "tool.finish"])

    facets = []
    for key, group in groups.items():
        # Each session is analyzed alone: `analyze` merges repeat counts over its input.
        # Policy findings such as a same-model subagent spawn are not trouble signals.
        # Only snapshots taken during the session's own span in the window can implicate
        # it: turn windows stay open after an unmatched turn start, so a session that
        # ended weeks ago would otherwise inherit every later oscillation on its checkout.
        span = [
            snapshot
            for snapshot in snapshots
            if group[0].received_at
            <= datetime.fromisoformat(str(snapshot["observed_at"]))
            <= group[-1].received_at + SNAPSHOT_GRACE
        ]
        shadow = [
            finding
            for finding in analysis.analyze(group, snapshots=span)["findings"]
            if finding["rule"] in analysis.RULES
        ]
        findings = [
            finding
            for finding in shadow
            if finding["rule"] != "diff_oscillation" or finding["session_ids"]
        ]
        facets.append(
            _facet(
                key,
                group,
                findings=findings,
                unattributed=len(shadow) - len(findings),
                label=labels[key],
                requests=series.get((key[0], key[1], None), []),
                loops=loops.get(key, []),
            )
        )
    items: list[dict[str, Any]] = [
        {"item_id": f"S{number}", **facet}
        for number, facet in enumerate(sorted(facets, key=_rank), 1)
    ]

    facts = {
        "provider": provider,
        "sessions_total": len(items),
        "sessions_by_provider": dict(Counter(item["provider"] for item in items)),
        "sessions_with_trouble": sum(
            1 for item in items if any(name != "no_end" for name in item["signals"])
        ),
        "sessions_ended": sum(1 for item in items if item["ended"]),
        "sessions_labelled": sum(1 for item in items if item["label"]["task_outcome"] != "unknown"),
        "signals": SIGNALS,
    }
    coverage = counts | {
        "events_without_session": sum(1 for event in events if event.session_id is None),
        "sessions_without_prompt": sum(1 for item in items if item["first_prompt"] is None),
        "sessions_without_usage": sum(1 for item in items if not item["requests"]),
        "sessions_without_end": sum(1 for item in items if not item["ended"]),
        "sessions_with_unattributed_oscillations": sum(1 for item in items if item[UNATTRIBUTED]),
        "sessions_unlabelled": sum(
            1 for item in items if item["label"]["task_outcome"] == "unknown"
        ),
        "notes": [
            "Items are sessions ranked by trouble, so a budget cut drops the calmest; "
            "facts count them all.",
            "A facet covers the window only; a session that started earlier shows its "
            "later part, and only diff snapshots taken during that part are considered.",
            "Content that expired or was never captured leaves prompts and messages empty, "
            "and loops and findings need captured tool input; unknown is not zero.",
            "A session without an observed end may still be running; it is not abandoned "
            "on that evidence alone.",
            "A diff oscillation without an observed turn boundary on its checkout cannot be "
            "attributed to one session; it is counted per session in "
            f"{UNATTRIBUTED}, not as a finding.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)


def resolve(
    entries: list[dict[str, Any]], fitted: dict[str, Any], project: str
) -> list[dict[str, Any]]:
    """Map cited item ids to provider and session id, so the model never retypes a UUID."""
    index = {item["item_id"]: item for item in fitted["items"]}
    resolved = []
    for entry in entries:
        found = [
            {"provider": item["provider"], "session_id": item["session_id"]}
            for item_id in entry.get("item_ids", [])
            if (item := index.get(item_id)) is not None
        ]
        extra: dict[str, Any] = {"sessions": found}
        if entry.get("state") and found:
            first = found[0]
            extra["open_with"] = (
                f"agent-watchdog insights session --project {project} "
                f"--provider {first['provider']} --session {first['session_id']}"
            )
        resolved.append(entry | extra)
    return resolved
