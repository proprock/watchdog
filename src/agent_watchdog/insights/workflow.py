"""`insights workflow`: repeated call sequences and polling loops worth automating."""

import json
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

MODE = "workflow"
LENGTHS = (3, 4, 5, 6)
MIN_OCCURRENCES = 3
MIN_SESSIONS = 2
LOOP_MIN = 3
TOP_SEQUENCES = 60
TOP_LOOPS = 40
EXAMPLES = 3
TOP_CLASSES = 25
_SEPARATOR = "\x1f"
# Reading, searching and editing are the work itself; a chain made only of these is
# not a candidate for automation.
ROUTINE = frozenset(
    {"Read", "Edit", "MultiEdit", "Write", "Grep", "Glob", "NotebookEdit", "apply_patch"}
    | {"TodoWrite", "update_plan", "view_image"}
)

PROMPT = """\
Mode: workflow. Tool calls are reduced to classes: the tool name, and for shell tools \
the program plus subcommand (for example "Bash: git diff"), never the arguments. \
"sequence" items are contiguous chains of classes that recur at least three times \
across at least two sessions, with example occurrences; "loop" items are runs of one \
class repeated back to back by one agent, such as polling a CI run or re-running the \
same command after small changes, with their span and whether the inputs were \
identical. "facts" gives the most used classes. Durations come from the provider \
and are missing for some tools.

Recommend, where the evidence supports it:
- skill: a reusable skill (Claude Code `.claude/skills/<name>/SKILL.md`, Codex skills) \
that tells the agent when and how to run the sequence.
- script: a script that runs the whole sequence or the polling loop deterministically \
and returns a compact result (for example wait for CI and print only the failures).
- slash_command: a user-invoked command for a sequence the user starts by hand.
- instruction: a CLAUDE.md or AGENTS.md rule when the sequence is a policy, such as \
checks to run before a commit.
- hook: a provider hook that runs a step automatically (for example formatting after \
an edit).
- other.
Put the SKILL.md, script, or instruction text in "draft". Estimate \
"estimated_calls_saved" from the occurrences, or null. Do not propose automating a \
sequence that is simply normal editing work.
"""


class WorkflowRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal["skill", "script", "slash_command", "instruction", "hook", "other"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    estimated_calls_saved: int | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[WorkflowRecommendation]
    rule_candidates: list[RuleCandidate]


class ScopedWorkflowRecommendation(WorkflowRecommendation, ScopeFields):
    pass


class CrossOutput(OutputModel):
    summary: str
    recommendations: list[ScopedWorkflowRecommendation]
    rule_candidates: list[RuleCandidate]


Call = tuple[Envelope, str]
# Back-to-back calls of one class, collapsed so a chain is a list of distinct steps.
Block = list[Call]
# Project alias (None for a single-project read), provider, session, agent.
AgentKey = tuple[str | None, str, str | None, str | None]


def _session(key: AgentKey) -> tuple[str | None, str, str | None]:
    return key[:3]


def _blocks(entries: list[tuple[str | None, Envelope]]) -> dict[AgentKey, list[Block]]:
    blocks: dict[AgentKey, list[Block]] = defaultdict(list)
    for alias, event in entries:
        tool_input, _response = timeline.tool_content(event)
        name = command_class(timeline.tool_name(event), tool_input)
        agent = blocks[(alias, event.provider, event.session_id, event.agent_id)]
        if agent and agent[-1][0][1] == name:
            agent[-1].append((event, name))
        else:
            agent.append([(event, name)])
    return blocks


def _inputs(block: Block) -> list[str]:
    return [json.dumps(timeline.tool_content(event)[0], sort_keys=True) for event, _ in block]


def _loops(blocks: dict[AgentKey, list[Block]]) -> list[tuple[AgentKey, Block]]:
    """Runs that repeat the same input: polling or blind re-running, not varied work."""
    loops = [
        (key, block)
        for key, agent in blocks.items()
        for block in agent
        if len(block) >= LOOP_MIN and len(set(_inputs(block))) <= len(block) // 2
    ]
    return sorted(loops, key=lambda loop: len(loop[1]), reverse=True)


def _patterns(
    blocks: dict[AgentKey, list[Block]],
) -> list[tuple[tuple[str, ...], list[tuple[AgentKey, int]]]]:
    found: dict[tuple[str, ...], list[tuple[AgentKey, int]]] = defaultdict(list)
    for key, agent in blocks.items():
        names = [block[0][1] for block in agent]
        for length in LENGTHS:
            for start in range(len(names) - length + 1):
                found[tuple(names[start : start + length])].append((key, start))
    frequent = {
        gram: places
        for gram, places in found.items()
        if len(places) >= MIN_OCCURRENCES
        and len({_session(key) for key, _start in places}) >= MIN_SESSIONS
        and not set(gram) <= ROUTINE
    }

    def joined(gram: tuple[str, ...]) -> str:
        return _SEPARATOR + _SEPARATOR.join(gram) + _SEPARATOR

    # Keep a chain only when no longer chain always contains it.
    maximal = [
        (gram, places)
        for gram, places in frequent.items()
        if not any(
            len(other) > len(gram)
            and len(other_places) >= len(places)
            and joined(gram) in joined(other)
            for other, other_places in frequent.items()
        )
    ]
    maximal.sort(key=lambda pair: len(pair[0]) * len(pair[1]), reverse=True)
    return maximal[:TOP_SEQUENCES]


def _brief(event: Envelope) -> dict[str, Any]:
    tool_input, _response = timeline.tool_content(event)
    return {"evidence_id": str(event.event_id), "input": excerpt(tool_input, 240)}


# One project's tool.finish events in the window.
Raw = list[Envelope]


def read(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Raw:
    _types, finishes = inspection.tool_finishes(
        paths, project, provider=provider, session_id=session_id, since=since, until=until
    )
    return finishes


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


def _owner(place: tuple[AgentKey, int]) -> str | None:
    return place[0][0]


def analyze(sources: Sequence[tuple[str | None, Raw]]) -> Draft:
    """Find chains and loops in one project (alias None) or across several tagged by alias.

    The thresholds apply to the union, so a chain too rare in every project alone still
    counts when the projects together repeat it.
    """
    tagged = any(alias is not None for alias, _raw in sources)
    entries = [(alias, event) for alias, finishes in sources for event in finishes]
    if len(sources) > 1:
        entries.sort(key=lambda entry: entry[1].received_at)
    blocks = _blocks(entries)
    patterns = _patterns(blocks)
    all_loops = _loops(blocks)
    loops = all_loops[:TOP_LOOPS]
    # Loops are per agent run and are not merged: each keeps its own project, and carries
    # how many loops of its class ran in each project, so the class ranks by spread.
    loop_projects: dict[str, Counter[str | None]] = defaultdict(Counter)
    for key, run in all_loops:
        loop_projects[run[0][1]][key[0]] += 1

    items: list[dict[str, Any]] = []
    for gram, places in patterns:
        occurrences = [blocks[key][start : start + len(gram)] for key, start in places]
        calls = [call for occurrence in occurrences for block in occurrence for call in block]
        durations = [timeline.duration_ms(event) for event, _name in calls]
        known = [value for value in durations if value is not None]
        item = {
            "kind": "sequence",
            "steps": list(gram),
            "occurrences": len(places),
            "sessions": len({_session(key) for key, _start in places}),
            "calls": len(calls),
            "wall_ms_known": sum(known),
            "duration_unknown_calls": len(durations) - len(known),
            "examples": [
                [_brief(block[0][0]) for block in blocks[key][start : start + len(gram)]]
                for key, start in share(places, EXAMPLES, _owner, newest=False)
            ],
        }
        if tagged:
            item["projects"] = dict(Counter(key[0] for key, _start in places))
        items.append(item)
    for key, run in loops:
        first, last = run[0][0], run[-1][0]
        durations = [timeline.duration_ms(event) for event, _name in run]
        item = {
            "kind": "loop",
            "class": run[0][1],
            "provider": first.provider,
            "session_id": first.session_id,
            "agent_id": first.agent_id,
            "calls": len(run),
            "distinct_inputs": len(set(_inputs(run))),
            "span_seconds": round((last.received_at - first.received_at).total_seconds()),
            "wall_ms_known": sum(value for value in durations if value is not None),
            "first": _brief(first),
            "last": _brief(last),
            "event_ids": [str(event.event_id) for event, _name in run[:20]],
        }
        if tagged:
            item["project"] = key[0]
            item["projects"] = dict(loop_projects[run[0][1]])
        items.append(item)
    # Seen in the most projects first (constant for one project), then the most calls.
    items.sort(key=lambda item: (len(item.get("projects", {})), item["calls"]), reverse=True)
    items = [{"item_id": f"W{index}", **item} for index, item in enumerate(items, 1)]

    classes = Counter(name for agent in blocks.values() for block in agent for _e, name in block)
    facts = {
        "tool_calls": len(entries),
        "agents": len(blocks),
        "sessions": len({_session(key) for key in blocks}),
        "distinct_classes": len(classes),
        "sequences_listed": len(patterns),
        "loops_listed": len(loops),
        "top_classes": dict(classes.most_common(TOP_CLASSES)),
    }
    if tagged:
        facts["calls_by_project"] = dict(Counter(alias for alias, _event in entries))
    coverage = {
        "calls_without_captured_input": sum(
            1 for _alias, event in entries if timeline.tool_content(event)[0] is None
        ),
        "notes": [
            "Classes ignore arguments, so one class can cover different targets; back-to-back "
            "calls of one class count as one step of a sequence.",
            f"A sequence needs {MIN_OCCURRENCES}+ occurrences in {MIN_SESSIONS}+ sessions and "
            f"a step beyond plain reading, searching and editing (top {TOP_SEQUENCES} listed); "
            f"a loop needs {LOOP_MIN}+ back-to-back calls of one "
            f"class by one agent, at least half repeating an earlier input (top {TOP_LOOPS}).",
            "Calls whose content expired or was not captured fall into an '(unknown)' shell class.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)
