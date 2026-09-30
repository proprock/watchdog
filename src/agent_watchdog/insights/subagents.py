"""`insights subagents`: what each delegated agent cost, returned, and ran on."""

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate

MODE = "subagents"
AGENT_TOOLS = frozenset({"Agent", "Task"})
FAMILIES = ("opus", "sonnet", "haiku", "fable")
TASK_EXCERPT = 800
RESULT_EXCERPT = 1500

PROMPT = """\
Mode: subagents. Each item is one delegated agent (a Claude subagent or a Codex \
agent): its type, the task it was given ("task", from the spawning call when \
captured), the model it ran on and the coordinator's model when it started, its own \
measured requests and tokens ("input_read_tokens" is all context it read), its tool \
calls and failures, its duration, and the result it handed back ("result_bytes" and an \
excerpt). "facts.by_type" aggregates per agent type. A subagent's own tokens are not \
added to the coordinator's context; only its result is.

Recommend, where the evidence supports it:
- model_routing: agent types or tasks that could run on a smaller model (Claude \
Code: `model:` in `.claude/agents/<name>.md` or the Agent tool's model parameter), or \
that failed and need a stronger one.
- delegate_more: coordinator work that a subagent could do and summarize.
- delegate_less: delegations whose cost or result size shows no benefit over doing \
the work inline.
- prompt_scope: task prompts that are too broad or ask for too long a result.
- agent_definition: a custom agent definition for a recurring task (draft its \
frontmatter and instructions).
- parallelism, or other.
Put agent definitions or instruction text in "draft".
"""


class SubagentRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal[
        "model_routing",
        "delegate_more",
        "delegate_less",
        "prompt_scope",
        "agent_definition",
        "parallelism",
        "other",
    ]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[SubagentRecommendation]
    rule_candidates: list[RuleCandidate]


def family(model: str | None) -> str | None:
    if model is None:
        return None
    return next((part for part in model.split("-") if part in FAMILIES), model)


def _model_at(sequence: list[timeline.Request], at_us: int) -> str | None:
    earlier = [request.model for request in sequence if request.at_us <= at_us and request.model]
    return earlier[-1] if earlier else None


def _spawn_calls(finishes: list[Envelope]) -> dict[str, Envelope]:
    """Map a Claude agent ID to the Agent tool call that launched it."""
    calls: dict[str, Envelope] = {}
    for event in finishes:
        if timeline.tool_name(event) not in AGENT_TOOLS:
            continue
        _input, response = timeline.tool_content(event)
        agent_id = response.get("agentId") if isinstance(response, dict) else None
        if isinstance(agent_id, str):
            calls[agent_id] = event
    return calls


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
        lifecycle = timeline.events(db, ("agent.start", "agent.end"), **options)
    agent_types, finishes = inspection.tool_finishes(paths, project, **options)
    launches = _spawn_calls(finishes)

    starts: dict[timeline.Key, Envelope] = {}
    ends: dict[timeline.Key, Envelope] = {}
    for event in lifecycle:
        if event.agent_id is None:
            continue
        key = (event.provider, event.session_id, event.agent_id)
        (starts if event.kind == "agent.start" else ends).setdefault(key, event)
    calls: dict[timeline.Key, list[Envelope]] = defaultdict(list)
    for event in finishes:
        if event.agent_id is not None:
            calls[(event.provider, event.session_id, event.agent_id)].append(event)
    keys = set(starts) | set(ends) | {key for key in series if key[2] is not None}

    items = []
    untyped_stops = 0
    for key in keys:
        provider_name, session, agent = key
        start, end = starts.get(key), ends.get(key)
        anchor = start or end
        requests = series.get(key, [])
        agent_calls = calls.get(key, [])
        launch = launches.get(agent) if agent else None
        # Claude Code also fires SubagentStop, with no type, for internal helpers that
        # never start, call tools, or report usage; they are not delegated work.
        if (
            end is not None
            and start is None
            and launch is None
            and not requests
            and not agent_calls
            and not timeline.metadata(end).get("agent_type")
        ):
            untyped_stops += 1
            continue
        tool_input = timeline.tool_content(launch)[0] if launch else None
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        response = timeline.tool_content(launch)[1] if launch else None
        response = response if isinstance(response, dict) else {}
        started_us = (
            timeline.us(anchor.received_at) if anchor else (requests[0].at_us if requests else 0)
        ) or 0
        coordinator = _model_at(series.get((provider_name, session, None), []), started_us)
        models = sorted({request.model for request in requests if request.model})
        result = timeline.content(end, "last_assistant_message") if end else None
        agent_type = (
            timeline.metadata(anchor).get("agent_type") if anchor else None
        ) or agent_types.get(agent or "")
        items.append(
            {
                "evidence_id": str(anchor.event_id) if anchor else requests[0].event_id,
                "launch_evidence_id": str(launch.event_id) if launch else None,
                "provider": provider_name,
                "session_id": session,
                "agent_id": agent,
                "agent_type": agent_type or "(unnamed)",
                "description": excerpt(tool_input.get("description"), 200),
                "task": excerpt(tool_input.get("prompt"), TASK_EXCERPT),
                "requested_model": tool_input.get("model"),
                "background": tool_input.get("run_in_background") or response.get("isAsync"),
                "models": models,
                "coordinator_model": coordinator,
                "same_family_as_coordinator": (
                    family(models[0]) == family(coordinator)
                    if len(models) == 1 and coordinator
                    else None
                ),
                "requests": len(requests),
                "input_read_tokens": sum(request.occupancy for request in requests),
                "output_tokens": sum(request.output or 0 for request in requests),
                "peak_tokens": max((request.occupancy for request in requests), default=None),
                "tool_calls": len(agent_calls),
                "tool_failures": sum(
                    1 for event in agent_calls if inspection.finish_status(event) == "failure"
                ),
                "tool_classes": dict(
                    Counter(timeline.tool_name(event) for event in agent_calls).most_common(8)
                ),
                "duration_s": round((end.received_at - start.received_at).total_seconds())
                if start and end
                else None,
                "observed": {"start": start is not None, "end": end is not None},
                "result_bytes": len(result.encode("utf-8")) if isinstance(result, str) else None,
                "result": excerpt(result, RESULT_EXCERPT),
            }
        )
    items.sort(key=lambda item: (item["input_read_tokens"], item["tool_calls"]), reverse=True)
    items = [{"item_id": f"A{index}", **item} for index, item in enumerate(items, 1)]
    facts = {"agents": len(items), "by_type": _by_type(items)}
    coverage = counts | {
        "ends_without_start": sum(1 for key in ends if key not in starts),
        "untyped_stops_excluded": untyped_stops,
        "agents_without_usage": sum(1 for item in items if item["requests"] == 0),
        "agents_without_launch_call": sum(1 for item in items if not item["launch_evidence_id"]),
        "notes": [
            "Claude fan-out spawns undercount SubagentStart relative to SubagentStop; an "
            "agent seen only at its end is still listed.",
            "An agent without usage rows has unknown tokens, not zero; Codex subagent usage is "
            "stored per agent only since WD-131.",
            "The spawning call is linked for Claude by the agent ID in its response; its task "
            "text needs captured content.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)


def _by_type(items: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[(item["provider"], item["agent_type"])].append(item)
    result = {}
    for (provider, agent_type), members in sorted(grouped.items()):
        durations = [item["duration_s"] for item in members if item["duration_s"] is not None]
        results = [item["result_bytes"] for item in members if item["result_bytes"] is not None]
        result[f"{provider}: {agent_type}"] = {
            "agents": len(members),
            "input_read_tokens": sum(item["input_read_tokens"] for item in members),
            "output_tokens": sum(item["output_tokens"] for item in members),
            "tool_calls": sum(item["tool_calls"] for item in members),
            "tool_failures": sum(item["tool_failures"] for item in members),
            "duration_s_median": round(median(durations)) if durations else None,
            "result_bytes_median": round(median(results)) if results else None,
            "models": dict(Counter(model for item in members for model in item["models"])),
            "same_family_as_coordinator": sum(
                1 for item in members if item["same_family_as_coordinator"]
            ),
        }
    return result
