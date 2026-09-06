"""Explicit project registration with Git common-directory identity."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from agent_watchdog.config import Config, Project, same_path


class RegistryError(ValueError):
    """A project could not be safely identified or changed."""


@dataclass(frozen=True)
class Checkout:
    root: Path
    git_common_dir: Path | None


@dataclass(frozen=True)
class Resolution:
    project_id: UUID
    checkout_id: UUID
    root: Path


def _fast_checkout(dot_git: Path) -> tuple[Path, Path] | None:
    """Resolve (toplevel, git-common-dir) from a `.git` entry without spawning git.

    Returns None for any layout this does not recognize, so ``discover`` falls
    back to ``git rev-parse`` unchanged (WD-022a Part 2.2).
    """
    toplevel = dot_git.parent
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        first = dot_git.read_text(encoding="utf-8").splitlines()[:1]
        if not first or not first[0].startswith("gitdir:"):
            return None
        target = Path(first[0][len("gitdir:") :].strip())
        git_dir = target if target.is_absolute() else toplevel / target
        if not git_dir.is_dir():
            return None
    else:
        return None
    commondir = git_dir / "commondir"
    if commondir.is_file():
        relative = Path(commondir.read_text(encoding="utf-8").strip())
        common = relative if relative.is_absolute() else git_dir / relative
    else:
        common = git_dir
    return toplevel, common


def discover(path: Path, *, timeout: float = 10) -> Checkout:
    try:
        root = path.resolve(strict=True)
        if not root.is_dir():
            raise RegistryError("Project path must be an existing directory")
        dot_git = next(
            (parent / ".git" for parent in (root, *root.parents) if (parent / ".git").exists()),
            None,
        )
        if dot_git is None:
            return Checkout(root, None)
        fast = _fast_checkout(dot_git)
        if fast is not None:
            toplevel, common = fast
            return Checkout(toplevel.resolve(strict=True), common.resolve(strict=True))
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
            ],
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=True,
        )
        paths = result.stdout.splitlines()
        if len(paths) != 2:
            raise RegistryError("Git returned an unsupported project layout")
        return Checkout(Path(paths[0]).resolve(strict=True), Path(paths[1]).resolve(strict=True))
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise RegistryError("Cannot resolve project path or Git metadata") from error


class Registry:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config if config is not None else Config()

    def _find(self, checkout: Checkout) -> Project | None:
        for project in self.config.projects:
            if checkout.git_common_dir is not None:
                if project.git_common_dir is not None and same_path(
                    checkout.git_common_dir, project.git_common_dir
                ):
                    return project
            elif project.git_common_dir is None and (
                same_path(checkout.root, project.root) or checkout.root.is_relative_to(project.root)
            ):
                return project
        return None

    def resolve(self, path: Path, *, timeout: float = 10) -> Resolution | None:
        checkout = discover(path, timeout=timeout)
        project = self._find(checkout)
        if project is None:
            return None
        root = checkout.root
        if checkout.git_common_dir is None or same_path(root, project.root):
            root = project.root
        return Resolution(project.id, uuid5(project.id, os.path.normcase(str(root))), root)

    def _replace(self, projects: tuple[Project, ...]) -> None:
        # Validate before changing state, including duplicate and overlap checks.
        try:
            updated = Config(defaults=self.config.defaults, projects=projects)
        except ValueError as error:
            raise RegistryError("Project registration conflicts with the registry") from error
        self.config = updated

    def add(self, path: Path) -> Project:
        checkout = discover(path)
        existing = self._find(checkout)
        if existing is not None:
            if checkout.git_common_dir is None and not same_path(checkout.root, existing.root):
                raise RegistryError("Overlapping non-Git project roots")
            return existing
        project = Project(id=uuid4(), root=checkout.root, git_common_dir=checkout.git_common_dir)
        self._replace((*self.config.projects, project))
        return project

    def remove(self, project_id: UUID) -> None:
        self._get(project_id)
        self._replace(
            tuple(project for project in self.config.projects if project.id != project_id)
        )

    def _get(self, project_id: UUID) -> Project:
        for project in self.config.projects:
            if project.id == project_id:
                return project
        raise RegistryError("Unknown project UUID")

    def relocate(self, project_id: UUID, path: Path) -> Project:
        project = self._get(project_id)
        if project.root.exists():
            raise RegistryError("Relocation requires the old root to be absent")
        checkout = discover(path)
        if (project.git_common_dir is None) != (checkout.git_common_dir is None):
            raise RegistryError("Relocation cannot change the project kind")
        relocated = Project(
            id=project.id,
            root=checkout.root,
            git_common_dir=checkout.git_common_dir,
            overrides=project.overrides,
        )
        self._replace(
            tuple(relocated if item.id == project_id else item for item in self.config.projects)
        )
        return relocated
