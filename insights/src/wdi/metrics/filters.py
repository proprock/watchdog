"""Session filter shared by all metric queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_RELATIVE = re.compile(r"^(\d+)d$")


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
        if not self.include_noise:
            clauses.append(f"{alias}.is_noise = 0")
        clause = " AND ".join(clauses) if clauses else "1 = 1"
        return clause, params
