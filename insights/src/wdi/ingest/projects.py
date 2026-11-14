"""Project identity: group sessions into projects across machines."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from wdi.db.repository import Repository


def _project_basename(path: str) -> str:
    windows_path = PureWindowsPath(path)
    if windows_path.drive or ("\\" in path and not path.startswith("/")):
        return windows_path.name.lower()
    return PurePosixPath(path).name.lower()


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
                key = _project_basename(name)
                self._alias_to_canonical[key] = canonical
        self._cache: dict[str, int] = {}

    def resolve(self, cwd: str | None) -> int | None:
        if not cwd:
            return None
        basename = _project_basename(cwd)
        if not basename:
            return None
        canonical = self._alias_to_canonical.get(basename, basename)
        project_id = self._cache.get(canonical)
        if project_id is None:
            project_id = self._repo.get_or_create_project(canonical)
            self._cache[canonical] = project_id
        self._repo.link_project_path(project_id, self._machine_id, cwd)
        return project_id
