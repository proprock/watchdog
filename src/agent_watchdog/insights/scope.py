"""Cross-project reads for `insights --all-projects` (WD-132).

Each registered project is read through its own read-only snapshot; the records are
unioned in memory and analyzed together, so a pattern that is too rare in every project
alone still counts. Nothing is written and no merged store exists (the WD-016 boundary).
"""

import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from math import ceil
from typing import Any

from agent_watchdog.config import Config, UserPaths, project_aliases
from agent_watchdog.insights.bundle import Draft
from agent_watchdog.storage import StorageError

CROSS_MODES = ("errors", "permissions", "workflow")
USER_FILES = ("~/.claude/CLAUDE.md", "~/.codex/AGENTS.md", "~/.claude/settings.json")
# The file a user-scope recommendation moves to when it is narrowed to one project.
PROJECT_FILES = {
    "~/.claude/CLAUDE.md": "CLAUDE.md",
    "~/.codex/AGENTS.md": "AGENTS.md",
    "~/.claude/settings.json": ".claude/settings.json",
}

ADDENDUM = """
Scope: this bundle spans several registered projects ("facts.projects" lists them; \
"coverage.projects_skipped" counts projects left out). Each item carries "projects": \
how many times it occurred in each project. Items seen in more projects come first.

Every recommendation also states where the change belongs:
- scope "user": the pattern is user-wide. Use it only when a cited item occurred in at \
least two projects. target_file is ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, or \
~/.claude/settings.json; project is null.
- scope "project": the pattern belongs to one project. target_file is that project's \
own file relative to its root (CLAUDE.md, AGENTS.md, or .claude/settings.json); \
project is its alias from the bundle.
A pattern seen in only one project is never proposed at user scope. Do not propose a \
user-level rule that names a path, command, or tool specific to one project.
"""


@dataclass(frozen=True)
class Selection:
    """Which registered projects the bundle covers; names stay out of the bundle when skipped."""

    included: list[str]
    disabled: list[str]
    unreadable: list[str]


def collect(
    module: Any,
    paths: UserPaths,
    config: Config,
    *,
    provider: str | None,
    since: Any,
    until: Any,
) -> tuple[list[tuple[str, Any]], Selection]:
    """Read every enabled project on its own snapshot; one that cannot be read, such as a
    database locked by the daemon, is counted instead of aborting the run."""
    aliases = project_aliases(config.projects)
    sources: list[tuple[str, Any]] = []
    disabled: list[str] = []
    unreadable: list[str] = []
    for project in config.projects:
        alias = aliases[project.id]
        if not project.overrides.apply(config.defaults).insights_llm_enabled:
            disabled.append(alias)
            continue
        try:
            raw = module.read(
                paths, project, provider=provider, session_id=None, since=since, until=until
            )
        except (StorageError, sqlite3.Error):
            unreadable.append(alias)
            continue
        sources.append((alias, raw))
    return sources, Selection([alias for alias, _raw in sources], disabled, unreadable)


def annotate(draft: Draft, selection: Selection) -> Draft:
    """Name the included projects and count the skipped ones, without naming those."""
    coverage = draft.coverage | {
        "projects_skipped": {
            "disabled": len(selection.disabled),
            "unreadable": len(selection.unreadable),
        },
        "notes": [
            *draft.coverage.get("notes", []),
            "Each project was read through its own read-only snapshot and merged in memory. "
            "Items rank by the number of projects they occurred in, then by frequency. A "
            "project with insights_llm_enabled = false, or without a readable database, is "
            "excluded and only counted.",
        ],
    }
    return replace(draft, facts=draft.facts | {"projects": selection.included}, coverage=coverage)


def share[T](
    members: Sequence[T],
    limit: int,
    project: Callable[[T], str | None],
    *,
    newest: bool = True,
) -> list[T]:
    """Keep ``limit`` members in total, at most a fair share per project, in original order.

    With one project this is ``members[-limit:]`` (or ``members[:limit]``).
    """
    owners = {project(member) for member in members}
    cap = ceil(limit / max(len(owners), 1))
    taken: Counter[str | None] = Counter()
    kept: list[T] = []
    for member in reversed(members) if newest else members:
        owner = project(member)
        if taken[owner] < cap:
            taken[owner] += 1
            kept.append(member)
    return kept[::-1] if newest else kept


def enforce(entries: list[dict[str, Any]], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the model's scope honest: user scope needs a cited item seen in two projects."""
    items = {
        item.get("cluster_id") or item.get("item_id"): item for item in bundle.get("items", [])
    }
    included = set(bundle.get("facts", {}).get("projects", []))
    result = []
    for entry in entries:
        ids = (*entry.get("cluster_ids", []), *entry.get("item_ids", []))
        cited = [items[key] for key in ids if key in items]
        seen = {alias for item in cited for alias in item.get("projects", {})}
        scope, target, project = entry["scope"], entry["target_file"], entry["project"]
        notes: list[str] = []
        if scope == "user":
            project = None
            if not any(len(item.get("projects", {})) >= 2 for item in cited):
                scope = "project"
                target = PROJECT_FILES.get(target, target)
                project = next(iter(seen)) if len(seen) == 1 else None
                notes.append(
                    "narrowed to project scope: no cited item occurred in two projects "
                    "(or none could be matched to the bundle)"
                )
            elif target not in USER_FILES:
                notes.append("target_file is not one of the user-level files")
        elif project not in included:
            project = next(iter(seen)) if len(seen) == 1 else None
            notes.append("project is not in the bundle; set from the cited items when unambiguous")
        result.append(
            entry
            | {"scope": scope, "target_file": target, "project": project}
            | {"scope_notes": notes}
        )
    return result
