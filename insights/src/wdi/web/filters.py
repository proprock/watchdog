"""Parse request query parameters into a SessionFilter."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Query

from wdi.metrics.filters import SessionFilter

_AGENTS = ("claude", "codex")


def parse_filter(
    agent: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    include_noise: Annotated[str | None, Query()] = None,
    branch: Annotated[str | None, Query()] = None,
    repository_changed: Annotated[Literal["", "true", "false"] | None, Query()] = None,
    instructions_changed: Annotated[Literal["", "true", "false"] | None, Query()] = None,
) -> SessionFilter:
    """Build a SessionFilter from query params; empty strings mean 'no filter'.

    An invalid ``agent`` is rejected; ``since`` is validated downstream by
    SessionFilter, so a malformed value simply matches nothing rather than 500ing.
    ``include_noise`` is checkbox-style: any non-empty value enables it.
    """
    if agent and agent not in _AGENTS:
        raise HTTPException(status_code=400, detail="agent must be 'claude' or 'codex'")
    return SessionFilter(
        agent=agent or None,
        project=project or None,
        since=since or None,
        include_noise=bool(include_noise),
        branch=branch or None,
        repository_changed=(repository_changed == "true") if repository_changed else None,
        instructions_changed=(instructions_changed == "true") if instructions_changed else None,
    )


def filter_query(flt: SessionFilter) -> str:
    """Re-encode a filter as a query string ('' or '?...') for building URLs."""
    params: dict[str, str] = {}
    if flt.agent:
        params["agent"] = flt.agent
    if flt.project:
        params["project"] = flt.project
    if flt.since:
        params["since"] = flt.since
    if flt.include_noise:
        params["include_noise"] = "1"
    if flt.branch:
        params["branch"] = flt.branch
    if flt.repository_changed is not None:
        params["repository_changed"] = str(flt.repository_changed).lower()
    if flt.instructions_changed is not None:
        params["instructions_changed"] = str(flt.instructions_changed).lower()
    return f"?{urlencode(params)}" if params else ""


FilterDep = Annotated[SessionFilter, Depends(parse_filter)]
