"""`insights permissions`: which tool calls stopped for approval, and for how long."""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate, ScopeFields
from agent_watchdog.insights.scope import share
from agent_watchdog.insights.tokens import command_class

MODE = "permissions"
EXAMPLES = 5
# A prompt belongs to the latest start of the named tool within this window before it.
MATCH_WINDOW_SECONDS = 600
# Prompts for these tools ask the user something; they are not permission gates.
INTERACTIVE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
_TOOL = re.compile(r"permission to use (\S+)")

PROMPT = """\
Mode: permissions. Each item groups the permission prompts for one tool class: the \
tool, and for shell tools the program plus subcommand. It lists how many prompts \
occurred, in how many sessions, under which permission modes, how long the user took \
(prompt to the call's finish, when the call finished), how the calls ended, and \
examples of the exact input that was gated. Items for AskUserQuestion or ExitPlanMode \
are questions to the user, not permission gates. "facts" gives the permission modes \
in use and the prompt rate per mode.

Recommend, where the evidence supports it:
- allow_rule: a Claude Code permission rule for `.claude/settings.json` \
(`permissions.allow`, for example `Bash(uv run pytest:*)`), scoped as narrowly as the \
examples allow. Classify its risk: read_only, mutating, or destructive. Never propose \
allowing destructive or network-changing commands broadly.
- deny_rule: a command that should never run.
- permission_mode: a different mode for this kind of work (default, acceptEdits, plan, \
auto).
- hook, or other.
Put the exact rule in "rule" and a settings snippet in "draft". Codex approval \
prompts are not observed through hooks, so give Codex advice only from its evidence.
"""


class PermissionRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal["allow_rule", "deny_rule", "permission_mode", "hook", "other"]
    rule: str | None
    risk: Literal["read_only", "mutating", "destructive", "not_applicable"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[PermissionRecommendation]
    rule_candidates: list[RuleCandidate]


class ScopedPermissionRecommendation(PermissionRecommendation, ScopeFields):
    pass


class CrossOutput(OutputModel):
    summary: str
    recommendations: list[ScopedPermissionRecommendation]
    rule_candidates: list[RuleCandidate]


# One project's permission-related events and the window's tool.finish events.
Raw = tuple[list[Envelope], list[Envelope]]


def _gated_start(prompt: Envelope, starts: list[Envelope]) -> Envelope | None:
    match = _TOOL.search(str(timeline.metadata(prompt).get("message") or ""))
    tool = match.group(1) if match else None
    for start in reversed(starts):
        if start.received_at > prompt.received_at:
            continue
        if (prompt.received_at - start.received_at).total_seconds() > MATCH_WINDOW_SECONDS:
            return None
        if tool is None or timeline.tool_name(start) == tool:
            return start
    return None


def _outcome(finish: Envelope | None) -> str:
    if finish is None:
        return "no_finish_observed"
    # A finish is itself the approval; only a failure signal says it went wrong.
    status = inspection.finish_status(finish)
    return {"failure": "failed", "interrupt": "interrupted"}.get(status, "finished")


def _tool_use_id(event: Envelope) -> str | None:
    value = timeline.namespace(event).get("tool_use_id") or timeline.metadata(event).get(
        "tool_use_id"
    )
    return value if isinstance(value, str) else None


def read(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Raw:
    options = {"provider": provider, "session_id": session_id, "since": since, "until": until}
    with inspection.database(paths, project) as db:
        events = timeline.events(db, ("tool.start", "waiting"), **options)
    _types, finishes = inspection.tool_finishes(paths, project, **options)
    return events, finishes


def build(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Draft:
    raw = read(paths, project, provider=provider, session_id=session_id, since=since, until=until)
    return analyze([(None, raw)])


def _owner(member: dict[str, Any]) -> str | None:
    return member["project"]


def analyze(sources: Sequence[tuple[str | None, Raw]]) -> Draft:
    """Group the permission prompts of one project (alias None) or of several tagged by alias."""
    tagged = any(alias is not None for alias, _raw in sources)
    tagged_events = [(alias, event) for alias, (events, _finishes) in sources for event in events]
    if len(sources) > 1:
        tagged_events.sort(key=lambda entry: entry[1].received_at)
    finished = {
        (alias, event.provider, event.session_id, _tool_use_id(event)): event
        for alias, (_events, finishes) in sources
        for event in finishes
        if _tool_use_id(event)
    }

    starts: dict[tuple, list[Envelope]] = defaultdict(list)
    modes: dict[str, Counter[str]] = defaultdict(Counter)
    prompts: list[tuple[str | None, Envelope]] = []
    for alias, event in tagged_events:
        if event.kind == "tool.start":
            starts[(alias, event.provider, event.session_id)].append(event)
            mode = timeline.metadata(event).get("permission_mode")
            modes[event.provider][str(mode) if mode else "(unknown)"] += 1
        elif timeline.metadata(event).get("notification_type") == "permission_prompt":
            prompts.append((alias, event))

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmatched = 0
    for alias, prompt in prompts:
        start = _gated_start(prompt, starts[(alias, prompt.provider, prompt.session_id)])
        if start is None:
            unmatched += 1
            continue
        tool_input = timeline.content(start, "tool_input")
        name = command_class(timeline.tool_name(start), tool_input)
        use_id = _tool_use_id(start)
        finish = finished.get((alias, start.provider, start.session_id, use_id)) if use_id else None
        groups[name].append(
            {
                "project": alias,
                "prompt": prompt,
                "start": start,
                "input": tool_input,
                "mode": timeline.metadata(start).get("permission_mode"),
                "waited_s": round((finish.received_at - prompt.received_at).total_seconds())
                if finish
                else None,
                "outcome": _outcome(finish),
            }
        )

    # Seen in the most projects first (constant for one project), then most prompted.
    ranked = sorted(
        groups.items(),
        key=lambda pair: (
            len({member["project"] for member in pair[1]} - {None}),
            len(pair[1]),
        ),
        reverse=True,
    )
    items = []
    for name, members in ranked:
        waits = sorted(member["waited_s"] for member in members if member["waited_s"] is not None)
        tool = timeline.tool_name(members[0]["start"])
        item = {
            "class": name,
            "tool": tool,
            "user_interaction": tool in INTERACTIVE_TOOLS,
            "prompts": len(members),
            "sessions": len(
                {(member["project"], member["prompt"].session_id) for member in members}
            ),
            "permission_modes": dict(Counter(str(member["mode"]) for member in members)),
            "waited_s": {
                "median": waits[len(waits) // 2] if waits else None,
                "max": waits[-1] if waits else None,
                "total": sum(waits),
                "unknown": len(members) - len(waits),
            },
            "outcomes": dict(Counter(member["outcome"] for member in members)),
            "examples": [
                {
                    "evidence_id": str(member["prompt"].event_id),
                    "tool_evidence_id": str(member["start"].event_id),
                    "input": excerpt(member["input"], 400),
                }
                | ({"project": member["project"]} if tagged else {})
                for member in share(members, EXAMPLES, _owner)
            ],
        }
        if tagged:
            item["projects"] = dict(Counter(member["project"] for member in members))
        items.append(item)
    items = [{"item_id": f"P{index}", **item} for index, item in enumerate(items, 1)]

    total_starts = {name: sum(counter.values()) for name, counter in modes.items()}
    facts = {
        "permission_prompts": len(prompts),
        "sessions_with_prompts": len(
            {(alias, event.provider, event.session_id) for alias, event in prompts}
        ),
        "tool_starts_by_mode": {name: dict(counter) for name, counter in modes.items()},
        "prompts_per_100_tool_starts": {
            name: round(
                100 * sum(1 for _alias, event in prompts if event.provider == name) / count, 2
            )
            for name, count in total_starts.items()
            if count
        },
    }
    if tagged:
        facts["prompts_by_project"] = dict(Counter(alias for alias, _event in prompts))
    coverage = {
        "prompts_without_matched_call": unmatched,
        "notes": [
            "A prompt is matched to the latest start of the tool its message names, within "
            f"{MATCH_WINDOW_SECONDS} seconds; the wait runs to that call's finish.",
            "A call with no observed finish may have been denied, interrupted, or lost; "
            "Watchdog cannot tell which.",
            "Codex approval prompts are not observed through hooks.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)
