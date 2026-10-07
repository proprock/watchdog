"""Shared builders for the insights test modules.

Each insights mode used to carry its own copy of the store factory, the CLI argv setup and
the ``insights.run`` option block; they differ only in a few defaults, so those defaults are
parameters here.
"""

import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from agent_watchdog import insights
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.storage import Store


def store(tmp_path, events):
    """Write `events` into a fresh one-project store and return ``(paths, project)``."""
    project = Project(id=uuid4(), root=tmp_path / "repo")
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    with Store(paths.project_data(project.id), project.id) as writer:
        for event in events:
            writer.put(event.model_copy(update={"project_id": project.id}))
    return paths, project


def invoke(monkeypatch, paths, *args) -> int:
    """Run the CLI in-process against `paths`; the caller reads the output."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-watchdog",
            "--config",
            str(paths.config),
            "--data",
            str(paths.data),
            "--runtime",
            str(paths.runtime),
            *args,
        ],
    )
    return main()


def runner_for(
    mode: str,
    *,
    provider: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
) -> Callable[..., Any]:
    """Build ``run(paths, project, runner, **overrides)`` for one insights mode."""

    def run(paths, project, runner, **overrides):
        options: dict[str, Any] = {
            "alias": "repo",
            "mode": mode,
            "provider": provider,
            "session_id": session_id,
            "since": since,
            "until": None,
            "model": "sonnet",
            "effort": None,
            "timeout": 60.0,
            "max_bundle_tokens": None,
            "language": "English",
            "dry_run": False,
            "output": None,
            "runner": runner,
        }
        return insights.run(paths, project, **(options | overrides))

    return run
