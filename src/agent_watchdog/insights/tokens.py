"""`insights tokens`: which tool calls fill the context, and whether they need to."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import PurePath
from statistics import median
from typing import Any, Literal

from agent_watchdog import inspection
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate

MODE = "tokens"
TOP_CALLS = 80
TOP_CLASSES = 40
TOP_REPEATS = 30
TOP_REBUILDS = 10
RESPONSE_HEAD = 1500
# A request that writes most of its context to the cache rebuilt an expired cache.
REBUILD_SHARE = 0.5
_SHELL_TOOLS = frozenset({"Bash", "PowerShell", "bash", "shell", "exec_command", "local_shell"})
_SUBCOMMANDS = frozenset(
    {"git", "gh", "docker", "cargo", "npm", "pnpm", "yarn", "uv", "kubectl", "dotnet", "go"}
)
_RUNNERS = frozenset({"run", "exec", "x", "tool"})
_PYTHONS = frozenset({"python", "python3", "py"})

PROMPT = """\
Mode: tokens. "appended" tokens are measured: the growth of one agent's context between \
two consecutive model requests, minus the earlier answer. Each growth is split evenly \
across the tool calls that finished in that interval ("attributed_tokens", inferred by \
time; growth with no tool call is prompts and provider reminders). "facts.by_class" \
aggregates tool calls by tool and, for shell tools, by command class (for example \
"Bash: uv run pytest"); "facts.repeats" are identical calls repeated in one session; \
"facts.cache_rebuilds" are Claude requests that rewrote most of their context to the \
prompt cache, usually after an idle gap longer than the cache lifetime; \
"facts.by_provider.input_read_tokens" is the total context read across all requests, \
the main cost driver. Items are the individual calls that added the most, with an \
excerpt of the input and the start of the response. "response_bytes" is the hook's \
copy of the result: for Claude Edit, Write and similar tools it includes the whole \
original file, which the model does not see, so rank by attributed tokens.

Recommend, where the evidence supports it:
- limit_output: flags or filters that shrink output (quiet test runners, `--stat`, \
`| head`, `rg -l`, narrower reads with offsets).
- wrapper_script: a script that runs a noisy command and returns a compact summary.
- delegate_to_subagent: work whose raw output the coordinator does not need, done by a \
subagent that returns a short summary.
- avoid_rereads: files read repeatedly in one session.
- prefer_other_tool: a more targeted tool (search instead of full reads, a symbol \
lookup instead of a file dump).
- cache: request timing that keeps the prompt cache warm.
- other.
Give "estimated_savings_tokens" as an estimate from the bundle's numbers, or null. Put \
commands, instruction text or scripts to paste in "draft".
"""


class TokenRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal[
        "limit_output",
        "wrapper_script",
        "delegate_to_subagent",
        "avoid_rereads",
        "prefer_other_tool",
        "cache",
        "other",
    ]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    estimated_savings_tokens: int | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[TokenRecommendation]
    rule_candidates: list[RuleCandidate]


def command_class(tool: str, tool_input: Any) -> str:
    """Group shell calls by program and subcommand, never by arguments."""
    if tool not in _SHELL_TOOLS:
        return tool
    command = tool_input.get("command") if isinstance(tool_input, dict) else tool_input
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str) or not command.strip():
        return f"{tool}: (unknown)"
    words = command.split()
    if words[0] == "cd" and "&&" in words:
        words = words[words.index("&&") + 1 :]
    while words and "=" in words[0] and not words[0].startswith(("-", "=")):
        words = words[1:]
    if words and words[0] == "&":  # PowerShell call operator
        words = words[1:]
    if not words:
        return f"{tool}: (unknown)"
    if words[0].startswith(("@'", '@"')):
        return f"{tool}: (here-string)"
    program = PurePath(words[0].strip("\"'").replace("\\", "/")).name.lower()
    program = program.removesuffix(".exe")
    arguments = [word for word in words[1:] if not word.startswith("-")]
    parts = [program]
    if program in _PYTHONS and "-m" in words[1:3]:
        module = words[words.index("-m") + 1 : words.index("-m") + 2]
        parts += ["-m", *module]
    elif program in _SUBCOMMANDS and arguments:
        parts.append(arguments[0])
        if arguments[0] in _RUNNERS and len(arguments) > 1:
            parts.append(PurePath(arguments[1].replace("\\", "/")).name)
    return f"{tool}: {' '.join(parts)}"


def _size(value: Any) -> int | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text.encode("utf-8"))


def _head(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return excerpt(text[:RESPONSE_HEAD])


def _percentiles(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p90": None, "max": None}
    return {
        "p50": ordered[(len(ordered) - 1) // 2],
        "p90": ordered[min(len(ordered) - 1, (9 * len(ordered)) // 10)],
        "max": ordered[-1],
    }


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
    _types, finishes = inspection.tool_finishes(paths, project, **options)
    steps, unattributed = timeline.steps(series, finishes)

    attributed: dict[str, tuple[int, int]] = {}
    growth_without_tools = 0
    for step in steps:
        # Across a reset the growth is unknown; otherwise a flat interval costs its calls 0.
        if step.reset:
            continue
        if not step.finishes:
            growth_without_tools += step.appended
            continue
        share = step.appended // len(step.finishes)
        for event in step.finishes:
            attributed[str(event.event_id)] = (share, len(step.finishes) - 1)

    classes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "tokens": [], "bytes": [], "response_unknown": 0, "sessions": set()}
    )
    calls = []
    repeats: dict[tuple, list[tuple[Envelope, int]]] = defaultdict(list)
    for event in finishes:
        tool = timeline.tool_name(event)
        tool_input, response = timeline.tool_content(event)
        name = command_class(tool, tool_input)
        stats = classes[name]
        stats["calls"] += 1
        stats["sessions"].add((event.provider, event.session_id))
        size = _size(response)
        if size is None:
            stats["response_unknown"] += 1
        else:
            stats["bytes"].append(size)
        tokens, shared = attributed.get(str(event.event_id), (None, 0))
        if tokens is not None:
            stats["tokens"].append(tokens)
            calls.append((tokens, shared, name, size, event, tool_input, response))
        if tool_input is not None:
            identity = (
                event.provider,
                event.session_id,
                tool,
                json.dumps(tool_input, sort_keys=True),
            )
            repeats[identity].append((event, tokens or 0))

    total_attributed = sum(sum(stats["tokens"]) for stats in classes.values())
    by_class = sorted(
        (
            {
                "class": name,
                "calls": stats["calls"],
                "attributed_calls": len(stats["tokens"]),
                "attributed_tokens": sum(stats["tokens"]),
                "share_of_attributed": round(sum(stats["tokens"]) / total_attributed, 3)
                if total_attributed
                else None,
                "tokens_per_call": _percentiles(stats["tokens"]),
                "response_bytes": {"sum": sum(stats["bytes"]), **_percentiles(stats["bytes"])},
                "response_unknown": stats["response_unknown"],
                "sessions": len(stats["sessions"]),
            }
            for name, stats in classes.items()
        ),
        key=lambda row: row["attributed_tokens"],
        reverse=True,
    )

    calls.sort(key=lambda call: call[0], reverse=True)
    items = [
        {
            "item_id": f"T{index}",
            "evidence_id": str(event.event_id),
            "provider": event.provider,
            "session_id": event.session_id,
            "agent_id": event.agent_id,
            "class": name,
            "attributed_tokens": tokens,
            "shared_with_calls": shared,
            "response_bytes": size,
            "input": excerpt(tool_input, 600),
            "response_head": _head(response),
        }
        for index, (tokens, shared, name, size, event, tool_input, response) in enumerate(
            calls[:TOP_CALLS], 1
        )
    ]
    repeated = sorted(
        (
            {
                "class": command_class(identity[2], json.loads(identity[3])),
                "session_id": identity[1],
                "count": len(members),
                "attributed_tokens": sum(tokens for _event, tokens in members),
                "input": excerpt(json.loads(identity[3]), 300),
                "event_ids": [str(event.event_id) for event, _tokens in members[:10]],
            }
            for identity, members in repeats.items()
            if len(members) > 1
        ),
        key=lambda row: row["attributed_tokens"],
        reverse=True,
    )
    facts = {
        "by_provider": _provider_totals(series),
        "appended_tokens": sum(step.appended for step in steps),
        "attributed_to_tool_calls": total_attributed,
        "appended_without_tool_calls": growth_without_tools,
        "by_class": by_class[:TOP_CLASSES],
        "repeats": repeated[:TOP_REPEATS],
        "cache_rebuilds": _cache_rebuilds(steps),
    }
    coverage = counts | {
        "tool_finishes": len(finishes),
        "tool_finishes_outside_request_intervals": len(unattributed),
        "response_unknown": sum(stats["response_unknown"] for stats in classes.values()),
        "classes_listed": min(len(by_class), TOP_CLASSES),
        "classes_total": len(by_class),
        "notes": [
            "Appended tokens are measured per agent; attribution to tool calls is inferred "
            "by time and split evenly across calls finishing in the same interval.",
            "Responses exist only while content is retained, and events over the payload "
            "limit (1 MiB) are dropped whole, so the largest outputs can be missing.",
            "Claude hook responses for Edit and Write include the original file, which the "
            "model does not see; response_bytes overstates their context cost.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)


def _provider_totals(series: dict[timeline.Key, list[timeline.Request]]) -> dict[str, Any]:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (provider, _session, agent), sequence in series.items():
        row = totals[provider]
        row["requests"] += len(sequence)
        row["subagent_requests"] += len(sequence) if agent else 0
        row["input_read_tokens"] += sum(request.occupancy for request in sequence)
        row["output_tokens"] += sum(request.output or 0 for request in sequence)
        row["cache_write_tokens"] += sum(request.cache_write or 0 for request in sequence)
    return {provider: dict(row) for provider, row in sorted(totals.items())}


def _cache_rebuilds(steps: list[timeline.Step]) -> dict[str, Any]:
    rebuilds = [
        step
        for step in steps
        if step.key[0] == "claude"
        and step.after.cache_write is not None
        and step.after.cache_write > step.after.occupancy * REBUILD_SHARE
    ]
    gaps = [(step.after.at_us - step.before.at_us) // 1_000_000 for step in rebuilds]
    largest = sorted(rebuilds, key=lambda step: step.after.cache_write or 0, reverse=True)
    return {
        "count": len(rebuilds),
        "rewritten_tokens": sum(step.after.cache_write or 0 for step in rebuilds),
        "gap_seconds_median": round(median(gaps)) if gaps else None,
        "examples": [
            {
                "evidence_id": step.after.event_id,
                "gap_seconds": (step.after.at_us - step.before.at_us) // 1_000_000,
                "rewritten_tokens": step.after.cache_write,
            }
            for step in largest[:TOP_REBUILDS]
        ],
    }
