"""`insights subagents`: what each delegated agent cost, returned, and ran on."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import attribution, timeline, window
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate

MODE = "subagents"
AGENT_TOOLS = frozenset({"Agent", "Task"})
CODEX_OPERATIONS = {
    "spawn_agent": "launch",
    "wait": "wait",
    "wait_agent": "wait",
    "send_input": "message",
    "send_message": "message",
    "followup_task": "message",
    "close_agent": "close",
    "interrupt_agent": "interrupt",
}
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

Requested overrides and inherited coordinator configuration are not observations \
of the child's resolved model or effort. Missing usage and results are unknown. \
Tokens without tariffs are unpriced; list-price estimates are never billed spend.

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


def _object(value: Any) -> dict[str, Any]:
    """Decode structured JSON only; code-mode strings remain opaque."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _identity(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _key(event: Envelope) -> timeline.Key:
    return event.provider, event.session_id, event.agent_id


def _operation(event: Envelope, names: dict[str, str]) -> str | None:
    if event.provider == "claude":
        return "launch" if timeline.tool_name(event) in AGENT_TOOLS else None
    if event.provider != "codex":
        return None
    execution = timeline.namespace(event).get("execution")
    evidence = execution.get("evidence_ids", []) if isinstance(execution, dict) else []
    original_names = [names.get(str(event.event_id)), *(names.get(str(e)) for e in evidence)]
    operations = set()
    for name in original_names:
        if not isinstance(name, str):
            continue
        for prefix in ("", "functions.", "collaboration."):
            if name.startswith(prefix) and name[len(prefix) :] in CODEX_OPERATIONS:
                operations.add(CODEX_OPERATIONS[name[len(prefix) :]])
    return operations.pop() if len(operations) == 1 else None


def _call_identity(event: Envelope) -> tuple:
    payload = timeline.namespace(event)
    metadata = timeline.metadata(event)
    native = payload.get("tool_use_id") or payload.get("call_id") or metadata.get("call_id")
    return (*_key(event), native or str(event.event_id))


@dataclass
class Launch:
    start: Envelope | None = None
    finish: Envelope | None = None
    child: str | None = None

    @property
    def anchor(self) -> Envelope:
        if self.start is not None:
            return self.start
        assert self.finish is not None
        return self.finish

    @property
    def arguments(self) -> dict[str, Any]:
        return _object(timeline.tool_content(self.anchor)[0])


def _spawn_calls(events: list[Envelope], names: dict[str, str]) -> list[Launch]:
    """Keep launches even when the result or child transcript is absent."""
    grouped: dict[tuple, Launch] = {}
    for event in events:
        if _operation(event, names) != "launch":
            continue
        launch = grouped.setdefault(_call_identity(event), Launch())
        if event.kind == "tool.start":
            launch.start = event
        elif event.kind == "tool.finish":
            launch.finish = event
            response = _object(timeline.tool_content(event)[1])
            launch.child = _identity(
                response.get("agentId" if event.provider == "claude" else "agent_id")
            )
    return list(grouped.values())


def _targets(event: Envelope, operation: str) -> list[str]:
    arguments = _object(timeline.tool_content(event)[0])
    response = _object(timeline.tool_content(event)[1])
    if operation == "wait":
        ids = arguments.get("ids")
        ids = ids if isinstance(ids, list) else []
        status = response.get("status")
        return list(
            dict.fromkeys(
                value
                for value in [*ids, *(status if isinstance(status, dict) else {})]
                if _identity(value)
            )
        )
    identity = _identity(arguments.get("id")) or _identity(arguments.get("target"))
    return [identity] if identity else []


def _operations(
    child: str | None,
    coordinator: timeline.Key | None,
    events: list[Envelope],
    names: dict[str, str],
) -> list[dict[str, Any]]:
    if child is None or coordinator is None:
        return []
    result: list[dict[str, Any]] = []
    observations: dict[tuple, Envelope] = {}
    for event in events:
        identity = _call_identity(event)
        if identity not in observations or event.kind == "tool.finish":
            observations[identity] = event
    for event in observations.values():
        operation = _operation(event, names)
        if (
            operation in {None, "launch"}
            or _key(event) != coordinator
            or child not in _targets(event, operation)
        ):
            continue
        response = _object(timeline.tool_content(event)[1])
        child_status = response.get("status")
        child_status = child_status.get(child) if isinstance(child_status, dict) else None
        completed = child_status.get("completed") if isinstance(child_status, dict) else None
        result.append(
            {
                "operation": operation,
                "at_us": attribution.at_us(event),
                "evidence_id": str(event.event_id),
                "observed_result": event.kind == "tool.finish",
                "result": excerpt(timeline.tool_content(event)[1], RESULT_EXCERPT),
                "completion_result": excerpt(completed, RESULT_EXCERPT),
                "completion_result_bytes": len(completed.encode("utf-8"))
                if isinstance(completed, str)
                else None,
                "outcome": inspection.finish_status(event),
            }
        )
    return sorted(result, key=lambda row: row["at_us"])


def _request_configurations(
    requests: list[timeline.Request],
    coordinator: timeline.Key | None,
    series: dict[timeline.Key, list[timeline.Request]],
    contexts: dict[timeline.Key, list[Envelope]],
) -> list[dict[str, Any]]:
    result = []
    for request in requests:
        config = attribution.request_configuration(request)
        parent = attribution.configuration_at(
            series.get(coordinator, []) if coordinator and request.native_time_available else [],
            contexts.get(coordinator, []) if coordinator and request.native_time_available else [],
            request.at_us,
        )
        result.append(
            config
            | {
                "coordinator_model": parent["model"],
                "coordinator_reasoning_effort": parent["reasoning_effort"],
                "coordinator_evidence_id": parent["evidence_id"],
                "same_model_as_coordinator": request.model == parent["model"]
                if request.model and parent["model"]
                else None,
                "same_reasoning_effort_as_coordinator": config["reasoning_effort"]
                == parent["reasoning_effort"]
                if config["reasoning_effort"] and parent["reasoning_effort"]
                else None,
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
    with window.database(paths, project) as db:
        series, counts = timeline.requests(db, **options)
        lookup_series = (
            timeline.requests(
                db, provider=provider, session_id=session_id, since=None, until=until
            )[0]
            if since is not None
            else series
        )
        all_events = timeline.events(
            db, None, provider=provider, session_id=session_id, since=None, until=None
        )
        # The execution projection strips tool namespaces. Validate the original
        # evidence before interpreting any derived call as a delegation operation.
        names = {
            str(original.event_id): timeline.tool_name(original)
            for original in window.source_events(db, provider, session_id)
            if original.kind in {"tool.start", "tool.finish"}
        }

    def inside(event: Envelope) -> bool:
        return (since is None or event.received_at >= since) and (
            until is None or event.received_at < until
        )

    tools = [event for event in all_events if event.kind in {"tool.start", "tool.finish"}]
    finishes = [event for event in tools if event.kind == "tool.finish" and inside(event)]
    launches = [
        launch
        for launch in _spawn_calls(tools, names)
        if inside(launch.anchor) or (launch.finish is not None and inside(launch.finish))
    ]
    contexts: dict[timeline.Key, list[Envelope]] = defaultdict(list)
    starts: dict[timeline.Key, Envelope] = {}
    ends: dict[timeline.Key, Envelope] = {}
    for event in sorted(all_events, key=attribution.at_us):
        key = _key(event)
        if attribution.context_configuration(event) is not None:
            contexts[key].append(event)
        if event.agent_id is not None and event.kind in {"agent.start", "agent.end"}:
            (starts if event.kind == "agent.start" else ends).setdefault(key, event)
    calls: dict[timeline.Key, list[Envelope]] = defaultdict(list)
    for event in finishes:
        if event.agent_id is not None:
            calls[_key(event)].append(event)
    visible_keys = {
        _key(event)
        for event in all_events
        if event.agent_id is not None
        and inside(event)
        and (
            event.kind in {"agent.start", "agent.end", "usage", "tool.finish"}
            or attribution.context_configuration(event) is not None
        )
    } | {key for key in series if key[2] is not None}
    by_child: dict[timeline.Key, list[Launch]] = defaultdict(list)
    for launch in launches:
        if launch.child is not None:
            by_child[(launch.anchor.provider, launch.anchor.session_id, launch.child)].append(
                launch
            )
    # A transcript lacking its coordinator identity cannot disambiguate a reused
    # child ID. Keep that transcript separate rather than charging it twice.
    records: list[tuple[timeline.Key, Launch | None, bool]] = []
    for launch in launches:
        key = (launch.anchor.provider, launch.anchor.session_id, launch.child)
        records.append((key, launch, launch.child is not None and len(by_child[key]) == 1))
    records.extend((key, None, True) for key in visible_keys if len(by_child.get(key, [])) != 1)

    items: list[dict[str, Any]] = []
    untyped_stops = 0
    for key, launch, attach_transcript in records:
        provider_name, session, agent = key
        start = starts.get(key) if attach_transcript else None
        end = ends.get(key) if attach_transcript else None
        requests = series.get(key, []) if attach_transcript else []
        child_contexts = (
            [event for event in contexts.get(key, []) if inside(event)] if attach_transcript else []
        )
        agent_calls = calls.get(key, []) if attach_transcript else []
        if (
            provider_name == "claude"
            and end is not None
            and start is None
            and launch is None
            and not requests
            and not agent_calls
            and not timeline.metadata(end).get("agent_type")
        ):
            untyped_stops += 1
            continue
        anchors = [value for value in (start, end, launch.anchor if launch else None) if value]
        anchors += child_contexts + agent_calls
        anchor = min(anchors, key=attribution.at_us) if anchors else None
        evidence_id = (
            str(anchor.event_id)
            if anchor
            else (
                requests[0].event_id
                if requests
                else str((child_contexts or agent_calls)[0].event_id)
            )
        )
        tool_input = launch.arguments if launch else {}
        response = (
            _object(timeline.tool_content(launch.finish)[1]) if launch and launch.finish else {}
        )
        coordinator_key: timeline.Key | None = None
        coordinator_source = "unknown"
        if launch:
            coordinator_key, coordinator_source = _key(launch.anchor), "launch"
        elif anchor and anchor.parent_agent_id is not None:
            coordinator_key = (provider_name, session, anchor.parent_agent_id)
            coordinator_source = "parent_agent_id"
        elif agent is not None:
            operators = {
                _key(event)
                for event in tools
                if event.provider == provider_name
                and event.session_id == session
                and (operation := _operation(event, names)) not in {None, "launch"}
                and agent in _targets(event, operation)
            }
            if len(operators) == 1:
                coordinator_key, coordinator_source = operators.pop(), "operation_target"
        coordinator_id = coordinator_key[2] if coordinator_key else None
        launch_at = (
            attribution.at_us(launch.anchor)
            if launch
            else (
                attribution.at_us(anchor)
                if anchor
                else requests[0].at_us
                if requests
                else attribution.at_us(child_contexts[0])
            )
        )
        native_launch_time = not (
            launch and launch.anchor.source == "transcript" and launch.anchor.occurred_at is None
        )
        parent = attribution.configuration_at(
            lookup_series.get(coordinator_key, [])
            if coordinator_key and native_launch_time
            else [],
            contexts.get(coordinator_key, []) if coordinator_key and native_launch_time else [],
            launch_at,
        )
        observed_configs = [
            value
            for event in child_contexts
            if (value := attribution.context_configuration(event)) is not None
        ]
        observed_configs += [attribution.request_configuration(request) for request in requests]
        models = sorted({config["model"] for config in observed_configs if config["model"]})
        efforts = sorted(
            {
                config["reasoning_effort"]
                for config in observed_configs
                if config["reasoning_effort"]
            }
        )
        operations = _operations(
            agent, coordinator_key, [event for event in tools if inside(event)], names
        )
        result = timeline.content(end, "last_assistant_message") if end else None
        result_id = str(end.event_id) if result is not None and end else None
        result_bytes = len(result.encode("utf-8")) if isinstance(result, str) else None
        if result is None:
            completed = [row for row in operations if row["completion_result"] is not None]
            if completed:
                result = completed[-1]["completion_result"]
                result_id = completed[-1]["evidence_id"]
                result_bytes = completed[-1]["completion_result_bytes"]
        agent_type = next(
            (
                timeline.metadata(value).get("agent_type")
                for value in (start, end)
                if value and timeline.metadata(value).get("agent_type")
            ),
            None,
        )
        input_tokens = sum(request.occupancy for request in requests) if requests else None
        output_tokens = (
            sum(request.output for request in requests if request.output is not None)
            if requests and all(request.output is not None for request in requests)
            else None
        )
        items.append(
            {
                "evidence_id": evidence_id,
                "launch_evidence_id": str(launch.anchor.event_id) if launch else None,
                "launch_result_evidence_id": str(launch.finish.event_id)
                if launch and launch.finish
                else None,
                "provider": provider_name,
                "session_id": session,
                "agent_id": agent,
                "coordinator_agent_id": coordinator_id,
                "coordinator_attribution": coordinator_source,
                "configuration_time_basis": "launch" if launch else "first_observation",
                "agent_type": agent_type
                or tool_input.get("agent_type")
                or tool_input.get("subagent_type")
                or "(unnamed)",
                "description": excerpt(
                    tool_input.get("description") or tool_input.get("task_name"), 200
                ),
                "task": excerpt(
                    tool_input.get("prompt") or tool_input.get("message"), TASK_EXCERPT
                ),
                "requested_model": tool_input.get("model"),
                "requested_reasoning_effort": tool_input.get("reasoning_effort"),
                "inherited_configuration": parent,
                "background": tool_input.get("run_in_background", response.get("isAsync")),
                "models": models,
                "reasoning_efforts": efforts,
                "observed_configurations": observed_configs,
                "request_configurations": _request_configurations(
                    requests, coordinator_key, lookup_series, contexts
                ),
                "coordinator_model": parent["model"],
                "coordinator_reasoning_effort": parent["reasoning_effort"],
                "same_family_as_coordinator": family(models[0]) == family(parent["model"])
                if len(models) == 1 and parent["model"]
                else None,
                "requests": len(requests) if requests else None,
                "input_read_tokens": input_tokens,
                "output_tokens": output_tokens,
                "peak_tokens": max((request.occupancy for request in requests), default=None),
                "tool_calls": len(agent_calls)
                if attach_transcript and (requests or agent_calls or start or end or child_contexts)
                else None,
                "tool_failures": sum(
                    1 for event in agent_calls if inspection.finish_status(event) == "failure"
                )
                if agent_calls
                else None,
                "tool_classes": dict(
                    Counter(timeline.tool_name(event) for event in agent_calls).most_common(8)
                ),
                "duration_s": round((attribution.at_us(end) - attribution.at_us(start)) / 1_000_000)
                if start and end
                else None,
                "observed": {
                    "start": start is not None,
                    "end": end is not None,
                    "launch": launch is not None,
                    "launch_result": launch is not None and launch.finish is not None,
                    "child_identity": agent is not None,
                    "usage": bool(requests),
                    "transcript": bool(requests or child_contexts),
                    "result": result is not None,
                },
                "operations": operations,
                "result_evidence_id": result_id,
                "result_bytes": result_bytes,
                "result": excerpt(result, RESULT_EXCERPT),
                "task_success": None,
                "unpriced_tokens": input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None,
                "pricing": {
                    "status": "unpriced",
                    "note": "No tariff applied; tokens are unpriced. "
                    "Any list-price estimate is not billed spend.",
                },
            }
        )
    items.sort(
        key=lambda item: (
            -(item["input_read_tokens"] or 0),
            -(item["tool_calls"] or 0),
            item["provider"],
            item["session_id"] or "",
            item["agent_id"] or "",
            item["evidence_id"],
        )
    )
    items = [{"item_id": f"A{index}", **item} for index, item in enumerate(items, 1)]
    facts = {"agents": len(items), "by_type": _by_type(items)}
    coverage = counts | {
        "ends_without_start": sum(1 for key in ends if key not in starts),
        "untyped_stops_excluded": untyped_stops,
        "agents_without_usage": sum(1 for item in items if not item["observed"]["usage"]),
        "agents_without_launch_call": sum(1 for item in items if not item["launch_evidence_id"]),
        "launches_without_identity": sum(1 for launch in launches if launch.child is None),
        "ambiguous_child_identities": sum(1 for values in by_child.values() if len(values) > 1),
        "agents_without_result": sum(1 for item in items if not item["observed"]["result"]),
        "notes": [
            "Launches and operations join only by explicit child identity "
            "within provider, session and coordinator.",
            "Missing usage, transcript or result is unknown, never zero. "
            "Coverage is limited to captured observations.",
            "Requested overrides and coordinator configuration do not establish "
            "the child's resolved model or effort.",
            "Stop, close and tool completion do not establish task success.",
            "No tariff applied; tokens are unpriced. List-price estimates are not billed spend.",
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
            "input_read_tokens": _known_sum(members, "input_read_tokens"),
            "output_tokens": _known_sum(members, "output_tokens"),
            "tool_calls": _known_sum(members, "tool_calls"),
            "tool_failures": _known_sum(members, "tool_failures"),
            "agents_without_usage": sum(not item["observed"]["usage"] for item in members),
            "duration_s_median": round(median(durations)) if durations else None,
            "result_bytes_median": round(median(results)) if results else None,
            "models": dict(Counter(model for item in members for model in item["models"])),
            "same_family_as_coordinator": sum(
                1 for item in members if item["same_family_as_coordinator"]
            ),
        }
    return result


def _known_sum(items: list[dict[str, Any]], field: str) -> int | None:
    values = [item[field] for item in items if item[field] is not None]
    return sum(values) if values else None
