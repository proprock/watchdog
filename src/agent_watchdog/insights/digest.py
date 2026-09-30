"""`insights digest`: one cross-mode synthesis of a project window (WD-134)."""

from datetime import UTC, datetime
from typing import Any, Literal

from agent_watchdog.config import Project, UserPaths
from agent_watchdog.insights import (
    bundle,
    context,
    errors,
    permissions,
    subagents,
    tokens,
    workflow,
)
from agent_watchdog.insights.bundle import Draft
from agent_watchdog.insights.contract import OutputModel, RuleCandidate

MODE = "digest"
# The digest reads the whole project window; a single session has `insights session`.
PROJECT_WIDE = True
MODULES: dict[str, Any] = {
    module.MODE: module for module in (errors, context, tokens, workflow, subagents, permissions)
}
# Each mode's fixed share of the bundle budget. A mode that has fewer items than its share
# holds leaves the rest unused (no redistribution), so one busy mode cannot crowd out another.
# The remaining 15% is headroom for the wrapper, which sizes each share from the same total.
SHARES = {
    "errors": 0.20,
    "context": 0.15,
    "tokens": 0.15,
    "workflow": 0.15,
    "subagents": 0.10,
    "permissions": 0.10,
}

PROMPT = """\
Mode: digest. The bundle looks at one project window through six modes, each with its own \
facts, coverage and ranked items: errors (E: clusters of failing tool calls), context (C: \
context growth and compactions), tokens (T: measured token cost by tool and command class), \
workflow (W: repeated command chains and loops), subagents (A: delegated agents), and \
permissions (P: permission prompts and waits). "facts.modes", "coverage.modes" and "items" \
are keyed by mode. Each mode holds a fixed share of the bundle budget and its items are \
ranked with the most important first, so a cut drops the least important ones \
("coverage.modes.<mode>.truncated"). A mode without items observed nothing to report; that \
is not proof that it has no problem.

Explain what only the combined view shows; the user can run each mode alone for its own \
findings:
- links: connections between modes, such as an environment failure (E) that causes retry \
loops (W) that inflate the context (C). State the mechanism in a sentence or two. Cite the \
item ids of every mode involved and the event ids that show it. A link must rest on \
evidence from at least two modes; a claim resting on one mode belongs in recommendations, \
not in links. Do not link findings that merely happen in the same window.
- recommendations: a short action list of at most 5, ranked across all modes with the most \
valuable first (its order is the rank). Prefer removing a cause shared by several modes over \
patching one symptom. Each cites the item ids and event ids it rests on. For kind \
"instruction", "settings" or "script", give the exact text or change in draft; otherwise \
null.
- rule_candidates are optional.
Recommend nothing when the evidence does not justify an action.
"""


class Link(OutputModel):
    title: str
    explanation: str
    item_ids: list[str]
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class DigestRecommendation(OutputModel):
    title: str
    item_ids: list[str]
    kind: Literal["instruction", "settings", "environment", "script", "split_task", "other"]
    target: Literal["claude", "codex", "both"]
    recommendation: str
    draft: str | None
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]


class Output(OutputModel):
    summary: str
    links: list[Link]
    recommendations: list[DigestRecommendation]
    rule_candidates: list[RuleCandidate]


def build(
    paths: UserPaths,
    project: Project,
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Draft:
    """Run each mode's own builder; facts and coverage are keyed by mode, items are tagged."""
    # Every builder reads its own snapshot, so a shared upper bound keeps them on one window.
    options = {
        "provider": provider,
        "session_id": session_id,
        "since": since,
        "until": until or datetime.now(UTC),
    }
    facts: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    for name, module in MODULES.items():
        draft = module.build(paths, project, **options)
        facts[name] = draft.facts
        coverage[name] = draft.coverage
        items += [{"mode": name} | item for item in draft.items]
    return Draft(facts=facts, coverage=coverage, items=items)


def fit(draft: Draft, *, mode: str, window: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Fit each mode into its own share, keeping every mode's facts and coverage."""
    facts: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    items: dict[str, list[dict[str, Any]]] = {}
    dropped = dropped_bytes = 0
    for name, share in SHARES.items():
        own = [
            {key: value for key, value in item.items() if key != "mode"}
            for item in draft.items
            if item["mode"] == name
        ]
        fitted = bundle.fit(
            Draft(draft.facts[name], draft.coverage[name], own),
            mode=name,
            window=window,
            max_tokens=int(max_tokens * share),
        )
        facts[name] = fitted["facts"]
        coverage[name] = fitted["coverage"]
        items[name] = fitted["items"]
        dropped += fitted["coverage"]["truncated"]["items"]
        dropped_bytes += fitted["coverage"]["truncated"]["bytes"]
    return {
        "mode": mode,
        "window": window,
        "facts": {"shares": SHARES, "modes": facts},
        "coverage": {
            "modes": coverage,
            "truncated": {"items": dropped, "bytes": dropped_bytes},
            "notes": [
                "Each mode was built from its own read-only snapshot up to one shared upper "
                "bound, then fitted into its fixed share of the budget; a mode's unused share "
                "is not given to another.",
                "A mode without items observed nothing to report; unknown is not zero.",
            ],
        },
        "items": items,
    }


def resolve(
    entries: list[dict[str, Any]], fitted: dict[str, Any], project: str
) -> list[dict[str, Any]]:
    """Name the modes whose item ids a link cites; fewer than two makes it single-mode.

    Only an item id belongs to exactly one mode. An event id often shows up in several
    modes (a failing call is an error sample, a loop step, and a token cost), so citing it
    grounds a link without saying which modes it connects. An id absent from the bundle
    supports no mode.
    """
    owners = {
        item.get("item_id") or item.get("cluster_id"): name
        for name, items in fitted["items"].items()
        for item in items
    }
    resolved = []
    for entry in entries:
        modes = sorted({owners[key] for key in entry.get("item_ids", []) if key in owners})
        resolved.append(entry | {"modes": modes, "cross_mode": len(modes) >= 2})
    return resolved
