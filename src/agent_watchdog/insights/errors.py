"""`insights errors`: cluster failing tool calls and ask why they recur and how to prevent them."""

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from agent_watchdog import inspection, privacy
from agent_watchdog.analysis import normalize_error_text
from agent_watchdog.config import Project, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.insights import timeline
from agent_watchdog.insights.bundle import Draft, excerpt
from agent_watchdog.insights.contract import OutputModel, RuleCandidate, ScopeFields
from agent_watchdog.insights.scope import share

MODE = "errors"
RECOVERY_WINDOW = 5
SAMPLES = 3
EVENT_IDS = 20

_EXIT_CODE_LINE = re.compile(r"\AExit code -?\d+\Z")
_EXCEPTION_LINE = re.compile(r"\A[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b")
_ERRORISH = re.compile(
    r"(?i)\b(?:error|exception|failed|failure|fatal|not found|denied|invalid|unexpected|"
    r"cannot|can't|no such|refused|timed out|timeout|missing|unknown|does not exist)\b"
)

PROMPT = """\
Mode: errors. The bundle groups failing tool calls into clusters ("items") by tool, \
exit code and a normalized key error line. Each cluster carries counts, the sessions and \
agent types involved, samples of the tool input and error text, and recoveries: the next \
successful call of the same tool by the same agent, which often shows what fixed it. \
"facts" gives totals for the whole window.

For each cluster worth acting on, decide:
- cause: environment (tooling, installation, OS or shell setup), agent_misuse (wrong \
tool, wrong shell syntax, a missing required argument, a wrong path assumption), \
false_positive (the provider flagged a failure although the output shows the command \
did what was intended, e.g. a check command that exits non-zero by design), \
real_failure (a genuine code or test failure that is part of normal work), or unknown.
- fix_kind: instruction (guidance for CLAUDE.md or AGENTS.md), settings (permissions, \
hooks, tool configuration), environment (fix the machine or project setup), script (a \
wrapper or helper that makes the failure impossible), or none.
- recommendation: the concrete change. For fix_kind "instruction", also give \
instruction_draft: the exact text to add; otherwise null.
Real failures during normal test-driven work usually need no action; never recommend \
fixing the user's product code. Merge clusters with one root cause into one \
recommendation listing all their cluster_ids. Cite the sample or recovery event ids \
that show the problem and its fix.
"""


class ErrorRecommendation(OutputModel):
    title: str
    cluster_ids: list[str]
    cause: Literal["environment", "agent_misuse", "false_positive", "real_failure", "unknown"]
    fix_kind: Literal["instruction", "settings", "environment", "script", "none"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    instruction_draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    recommendations: list[ErrorRecommendation]
    rule_candidates: list[RuleCandidate]


class ScopedErrorRecommendation(ErrorRecommendation, ScopeFields):
    pass


class CrossOutput(OutputModel):
    summary: str
    recommendations: list[ScopedErrorRecommendation]
    rule_candidates: list[RuleCandidate]


# One project's subagent types by agent id and the window's tool.finish events.
Raw = tuple[dict[str, str], list[Envelope]]


def key_line(text: str) -> str:
    """Pick the line that names the failure, then drop paths, numbers and quoted values."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and _EXIT_CODE_LINE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return ""
    if any(line.startswith("Traceback (most recent call last)") for line in lines):
        chosen = next((line for line in reversed(lines) if _EXCEPTION_LINE.match(line)), lines[-1])
    else:
        chosen = next((line for line in lines if _ERRORISH.search(line)), lines[0])
    return normalize_error_text(chosen)[:160]


def _agent(alias: str | None, event: Envelope) -> tuple:
    return (alias, event.provider, event.session_id, event.agent_id)


def _owner(record: dict[str, Any]) -> str | None:
    return record.get("project")


def read(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Raw:
    return inspection.tool_finishes(
        paths, project, provider=provider, session_id=session_id, since=since, until=until
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
    raw = read(paths, project, provider=provider, session_id=session_id, since=since, until=until)
    return analyze([(None, raw)])


def analyze(sources: Sequence[tuple[str | None, Raw]]) -> Draft:
    """Cluster the failures of one project (alias None) or of several tagged by alias."""
    tagged = any(alias is not None for alias, _raw in sources)
    agent_types = {alias: types for alias, (types, _finishes) in sources}
    entries = [(alias, event) for alias, (_types, finishes) in sources for event in finishes]
    if len(sources) > 1:
        entries.sort(key=lambda entry: entry[1].received_at)
    finishes = [event for _alias, event in entries]
    statuses = [inspection.finish_status(event) for event in finishes]
    later: dict[tuple, list[tuple[Envelope, str]]] = {}
    for (alias, event), status in zip(entries, statuses, strict=True):
        later.setdefault(_agent(alias, event), []).append((event, status))
    position = {
        event.event_id: index
        for sequence in later.values()
        for index, (event, _status) in enumerate(sequence)
    }

    clusters: dict[tuple, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}
    for (alias, event), status in zip(entries, statuses, strict=True):
        if status != "failure":
            continue
        record = inspection.error_record(event, agent_types[alias])
        if tagged:
            record["project"] = alias
        error_text = privacy.text(record["error"] or "")
        signature = (record["tool_name"], record["exit_code"], key_line(error_text))
        recovery = None
        sequence = later[_agent(alias, event)]
        start = position[event.event_id] + 1
        for candidate, candidate_status in sequence[start : start + RECOVERY_WINDOW]:
            fix = inspection.error_record(candidate, agent_types[alias])
            if fix["tool_name"] != record["tool_name"]:
                continue
            if timeline.succeeded(candidate, candidate_status):
                recovery = {
                    "evidence_id": fix["event_id"],
                    "failed_evidence_id": record["event_id"],
                    "tool_input": excerpt(fix["tool_input"]),
                }
                if tagged:
                    recovery["project"] = alias
            break
        clusters.setdefault(signature, []).append((record, recovery))

    # Seen in the most projects first (constant for one project), then most frequent, then
    # most recent; stored times are UTC ISO strings.
    ranked = sorted(
        clusters.items(),
        key=lambda pair: (
            len({_owner(record) for record, _ in pair[1]} - {None}),
            len(pair[1]),
            max(record["received_at"] for record, _ in pair[1]),
        ),
        reverse=True,
    )
    items = [
        _cluster(f"E{index}", signature, members)
        for index, (signature, members) in enumerate(ranked, start=1)
    ]
    failures = [record for members in clusters.values() for record, _ in members]
    counts = Counter(
        "success" if timeline.succeeded(event, status) else status
        for event, status in zip(finishes, statuses, strict=True)
    )
    facts = {
        "tool_finishes": len(finishes),
        "failures": len(failures),
        "successes": counts["success"],
        "clusters": len(items),
        "sessions_with_failures": len(
            {(_owner(r), r["provider"], r["session_id"]) for r in failures}
        ),
        "by_tool": dict(Counter(str(r["tool_name"]) for r in failures).most_common()),
        "by_provider": dict(Counter(r["provider"] for r in failures).most_common()),
        "by_agent_type": dict(
            Counter(r["agent_type"] or "coordinator" for r in failures).most_common()
        ),
    }
    if tagged:
        facts["by_project"] = dict(Counter(r["project"] for r in failures).most_common())
    coverage = {
        "unknown_outcome": counts["unknown"],
        "unclassified_outcome": counts["unclassified"],
        "interrupts_excluded": counts["interrupt"],
        "content_expired": counts["expired"],
        "notes": [
            "A failure is the provider's PostToolUseFailure signal or a failing captured "
            "tool_response; rows whose content expired or was never captured cannot be judged.",
            "Recoveries look at most five later calls by the same agent and only for the "
            "same tool; a fix through a different tool is not linked.",
        ],
    }
    return Draft(facts=facts, coverage=coverage, items=items)


def _cluster(
    cluster_id: str,
    signature: tuple,
    members: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> dict[str, Any]:
    records = [record for record, _ in members]
    recoveries = [recovery for _, recovery in members if recovery is not None]
    tool_name, exit_code, key = signature
    # The most recent samples reflect the current environment best; with several projects
    # each keeps a fair share.
    samples = [
        {
            "evidence_id": record["event_id"],
            "provider": record["provider"],
            "agent_type": record["agent_type"] or "coordinator",
            "exit_code": record["exit_code"],
            "tool_input": excerpt(record["tool_input"]),
            "error": excerpt(record["error"]),
        }
        | ({"project": record["project"]} if "project" in record else {})
        for record in share(records, SAMPLES, _owner)
    ]
    item = {
        "cluster_id": cluster_id,
        "signature": {"tool_name": tool_name, "exit_code": exit_code, "key_line": key},
        "count": len(records),
        "sessions": len({(_owner(r), r["provider"], r["session_id"]) for r in records}),
        "agent_types": dict(Counter(r["agent_type"] or "coordinator" for r in records)),
        "first_seen": records[0]["received_at"],
        "last_seen": records[-1]["received_at"],
        "recovered": len(recoveries),
        "event_ids": [
            record["event_id"] for record in share(records, EVENT_IDS, _owner, newest=False)
        ],
        "samples": samples,
        "recoveries": share(recoveries, SAMPLES, _owner),
    }
    if "project" in records[0]:
        item["projects"] = dict(Counter(record["project"] for record in records))
    return item
