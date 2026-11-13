"""Session filter shared by all metric queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_RELATIVE = re.compile(r"^(\d+)d$")


def environment_source_condition(session_alias: str, env_alias: str) -> str:
    return f"""(COALESCE(substr({session_alias}.source_path, 1, 5), '') != 'wd://'
        OR instr({session_alias}.source_path,
            'wd://' || {env_alias}.machine_id || '/' || {env_alias}.project_id || '/' ||
            {env_alias}.provider || '/' || {env_alias}.session_id || '/') = 1)"""


def environment_value(alias: str, column: str) -> str:
    return f"""(SELECT e.{column} FROM session_env e
        WHERE e.machine_id = {alias}.machine_id AND e.provider = {alias}.agent
          AND e.session_id = {alias}.session_id
          AND {environment_source_condition(alias, "e")}
        ORDER BY e.captured_at DESC, e.project_id, e.store_path LIMIT 1)"""


def outcome_value(alias: str, column: str) -> str:
    return f"""(SELECT o.{column} FROM checkout_states o
        WHERE o.machine_id = {alias}.machine_id AND o.provider = {alias}.agent
          AND o.session_id = {alias}.session_id
          AND o.project_id = {environment_value(alias, "project_id")}
          AND o.store_path = {environment_value(alias, "store_path")}
          AND o.checkout_id = {environment_value(alias, "checkout_id")}
        ORDER BY o.observed_at DESC LIMIT 1)"""


def instructions_changed_sql(alias: str) -> str:
    current = environment_value(alias, "files")
    previous = f"""(SELECT {environment_value("p", "files")} FROM sessions p
        WHERE p.project_id = {alias}.project_id AND p.project_id IS NOT NULL
          AND p.started_at < {alias}.started_at
        ORDER BY p.started_at DESC, p.session_id DESC LIMIT 1)"""
    return f"""(SELECT CASE
        WHEN current_files IS NULL OR previous_files IS NULL THEN NULL
        WHEN EXISTS (SELECT 1 FROM json_each(current_files) c
            LEFT JOIN json_each(previous_files) p
              ON json_extract(c.value, '$.path') = json_extract(p.value, '$.path')
              AND json_extract(c.value, '$.role') = json_extract(p.value, '$.role')
            WHERE p.value IS NULL OR (json_extract(c.value, '$.error') IS NULL
              AND json_extract(p.value, '$.error') IS NULL
              AND json_extract(c.value, '$.digest') IS NOT NULL
              AND json_extract(p.value, '$.digest') IS NOT NULL
              AND json_extract(c.value, '$.digest') != json_extract(p.value, '$.digest')))
          OR EXISTS (SELECT 1 FROM json_each(previous_files) p
            LEFT JOIN json_each(current_files) c
              ON json_extract(c.value, '$.path') = json_extract(p.value, '$.path')
              AND json_extract(c.value, '$.role') = json_extract(p.value, '$.role')
            WHERE c.value IS NULL) THEN 1
        WHEN EXISTS (SELECT 1 FROM json_each(current_files)
          WHERE json_extract(value, '$.digest') IS NULL
             OR json_extract(value, '$.error') IS NOT NULL)
          OR EXISTS (SELECT 1 FROM json_each(previous_files)
            WHERE json_extract(value, '$.digest') IS NULL
               OR json_extract(value, '$.error') IS NOT NULL) THEN NULL
        ELSE 0 END FROM (SELECT {current} AS current_files, {previous} AS previous_files))"""


def repository_changed_sql(alias: str) -> str:
    files = outcome_value(alias, "files_changed")
    untracked = outcome_value(alias, "untracked")
    base = outcome_value(alias, "base_head")
    head = outcome_value(alias, "head")
    return f"""CASE WHEN {files} > 0 OR {untracked} > 0
        OR ({base} IS NOT NULL AND {head} IS NOT NULL AND {base} != {head}) THEN 1
        WHEN {files} = 0 AND {untracked} = 0 AND {base} IS NOT NULL
            AND {head} IS NOT NULL AND {base} = {head} THEN 0 ELSE NULL END"""


def _normalize_since(since: str) -> str:
    """Accept an ISO date (2026-06-01) or a relative span (7d) -> ISO date string."""
    match = _RELATIVE.match(since.strip())
    if match:
        cutoff = datetime.now(UTC) - timedelta(days=int(match.group(1)))
        return cutoff.date().isoformat()
    return since.strip()


@dataclass(slots=True)
class SessionFilter:
    agent: str | None = None
    project: str | None = None
    since: str | None = None
    branch: str | None = None
    repository_changed: bool | None = None
    instructions_changed: bool | None = None
    # Background one-shot sessions (sessions.is_noise) are hidden by default.
    include_noise: bool = False

    def where(self, alias: str = "s") -> tuple[str, list[object]]:
        """Build a WHERE clause (without the keyword) and its parameters."""
        clauses: list[str] = []
        params: list[object] = []
        if self.agent:
            clauses.append(f"{alias}.agent = ?")
            params.append(self.agent)
        if self.project:
            clauses.append(
                f"{alias}.project_id IN (SELECT project_id FROM projects WHERE canonical_name = ?)"
            )
            params.append(self.project)
        if self.since:
            clauses.append(f"{alias}.started_at >= ?")
            params.append(_normalize_since(self.since))
        if self.branch:
            clauses.append(
                f"CASE WHEN {environment_value(alias, 'captured_at')} IS NULL "
                f"THEN {alias}.git_branch ELSE {environment_value(alias, 'branch')} END = ?"
            )
            params.append(self.branch)
        if self.repository_changed is not None:
            clauses.append(f"({repository_changed_sql(alias)}) = ?")
            params.append(int(self.repository_changed))
        if self.instructions_changed is not None:
            clauses.append(f"({instructions_changed_sql(alias)}) = ?")
            params.append(int(self.instructions_changed))
        if not self.include_noise:
            clauses.append(f"{alias}.is_noise = 0")
        clause = " AND ".join(clauses) if clauses else "1 = 1"
        return clause, params
