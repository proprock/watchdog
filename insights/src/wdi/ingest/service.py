"""Import orchestration shared by CLI commands."""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable

from wdi.config import Settings
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.ingest.claude_code import import_claude_source
from wdi.ingest.codex import import_codex_source
from wdi.ingest.models import ImportProgress, ImportStats
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.watchdog import import_watchdog


def run_import(
    settings: Settings,
    *,
    full: bool = False,
    on_progress: Callable[[ImportProgress], None] | None = None,
) -> dict[str, ImportStats]:
    """Import all enabled sources; returns per-source stats keyed by agent name."""
    conn = connect(settings.db_path)
    try:
        repo = Repository(conn)
        repo.ensure_machine(
            settings.machine_id, hostname=socket.gethostname(), os_name=sys.platform
        )
        if full:
            repo.reset_import_state(settings.machine_id, files_only=True)
        resolver = ProjectResolver(repo, settings.machine_id, settings.projects.aliases)

        results: dict[str, ImportStats] = {}
        if settings.watchdog.data_dirs:
            results["watchdog"] = import_watchdog(
                repo,
                settings.watchdog.data_dirs,
                settings.projects.aliases,
                full=full,
                on_progress=on_progress,
            )
        claude = settings.sources.claude_code
        if claude.enabled and claude.root.is_dir():
            results["claude"] = import_claude_source(
                repo, resolver, claude.root, settings.machine_id, on_progress=on_progress
            )
        codex = settings.sources.codex
        if codex.enabled and codex.root.is_dir():
            results["codex"] = import_codex_source(
                repo, resolver, codex.root, settings.machine_id, on_progress=on_progress
            )
        return results
    finally:
        conn.close()
