"""Project identity: group sessions into projects across machines."""

from __future__ import annotations

from pathlib import PurePath

from wdi.db.repository import Repository


class ProjectResolver:
    """Maps a session cwd to a project_id using configured aliases.

    Default canonical name is the lowercased basename of cwd; the config maps
    canonical -> [alias names or paths] to merge differently named checkouts.
    """

    def __init__(self, repo: Repository, machine_id: str, aliases: dict[str, list[str]]) -> None:
        self._repo = repo
        self._machine_id = machine_id
        self._alias_to_canonical: dict[str, str] = {}
        for canonical, names in aliases.items():
            for name in names:
                key = PurePath(name).name.lower()
                self._alias_to_canonical[key] = canonical
        self._cache: dict[str, int] = {}

    def resolve(self, cwd: str | None) -> int | None:
        if not cwd:
            return None
        basename = PurePath(cwd).name.lower()
        if not basename:
            return None
        canonical = self._alias_to_canonical.get(basename, basename)
        project_id = self._cache.get(canonical)
        if project_id is None:
            project_id = self._repo.get_or_create_project(canonical)
            self._cache[canonical] = project_id
        self._repo.link_project_path(project_id, self._machine_id, cwd)
        return project_id
