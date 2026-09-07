"""Versioned user configuration; no directories are created during reads."""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

import tomli_w
from platformdirs import PlatformDirs
from pydantic import Field, ValidationError, field_validator, model_validator

from agent_watchdog.files import atomic_write
from agent_watchdog.models import Positive, StrictModel, Versioned


class ConfigError(ValueError):
    """Configuration could not be read or validated."""


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left == right


class Limits(StrictModel):
    capture_content: bool = True
    reserve_bytes: Positive = 1024**2
    content_days: Positive = 30
    metrics_days: Positive = 180
    project_bytes: Positive = 2 * 1024**3
    inbox_bytes: Positive = 64 * 1024**2
    payload_bytes: Positive = 1024**2
    log_files: Positive = 5
    log_bytes: Positive = 10 * 1024**2
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def ordered_quotas(self) -> Self:
        if not self.payload_bytes <= self.inbox_bytes <= self.project_bytes:
            raise ValueError("Require payload_bytes <= inbox_bytes <= project_bytes")
        return self


class Overrides(StrictModel):
    capture_content: bool | None = None
    reserve_bytes: Positive | None = None
    content_days: Positive | None = None
    metrics_days: Positive | None = None
    project_bytes: Positive | None = None
    inbox_bytes: Positive | None = None
    payload_bytes: Positive | None = None

    def apply(self, defaults: Limits) -> Limits:
        return Limits.model_validate(defaults.model_dump() | self.model_dump(exclude_none=True))


class Project(StrictModel):
    id: UUID
    root: Path
    git_common_dir: Path | None = None
    overrides: Overrides = Field(default_factory=Overrides)

    @field_validator("root", "git_common_dir")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("Registered paths must be absolute and normalized")
        return value


class Config(Versioned):
    defaults: Limits = Field(default_factory=Limits)
    projects: tuple[Project, ...] = ()
    auto_add_projects: bool = False
    trusted_projects_dir: Path | None = None

    @field_validator("trusted_projects_dir")
    @classmethod
    def trusted_directory(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        try:
            expanded = value.expanduser()
        except RuntimeError as error:
            raise ValueError("Trusted projects directory cannot expand home") from error
        if not expanded.is_absolute() or ".." in expanded.parts:
            raise ValueError("Trusted projects directory must be absolute and normalized")
        return expanded.resolve(strict=False)

    @model_validator(mode="after")
    def unique_projects(self) -> Self:
        if self.auto_add_projects and (
            self.trusted_projects_dir is None or not self.trusted_projects_dir.is_dir()
        ):
            raise ValueError("Auto-add requires an existing trusted projects directory")
        for index, project in enumerate(self.projects):
            project.overrides.apply(self.defaults)
            for other in self.projects[:index]:
                same_common = (
                    project.git_common_dir is not None
                    and other.git_common_dir is not None
                    and same_path(project.git_common_dir, other.git_common_dir)
                )
                if project.id == other.id or same_path(project.root, other.root) or same_common:
                    raise ValueError("Duplicate project identity")
                if project.git_common_dir is None and other.git_common_dir is None:
                    if project.root.is_relative_to(other.root) or other.root.is_relative_to(
                        project.root
                    ):
                        raise ValueError("Overlapping non-Git project roots")
        return self


@dataclass(frozen=True)
class UserPaths:
    config: Path
    data: Path
    runtime: Path

    def project_data(self, project_id: UUID) -> Path:
        return self.data / "projects" / str(project_id)


def user_paths() -> UserPaths:
    dirs = PlatformDirs("agent-watchdog", appauthor=False, ensure_exists=False)
    return UserPaths(
        dirs.user_config_path / "config.toml", dirs.user_data_path, dirs.user_runtime_path
    )


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        # JSON permits serialized UUID/path strings while strict scalar validation stays enabled.
        return Config.model_validate_json(json.dumps(data, allow_nan=False))
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError, TypeError) as error:
        raise ConfigError("Invalid or unreadable configuration; file left unchanged") from error


def save_config(path: Path, config: Config) -> None:
    """Atomically replace a valid config. Callers must serialize registry mutations."""
    try:
        load_config(path)  # Never overwrite a corrupt or unsupported existing schema.
        validated = Config.model_validate_json(config.model_dump_json())
        content = tomli_w.dumps(validated.model_dump(mode="json", exclude_none=True))
        atomic_write(path, content.encode("utf-8"))
    except (OSError, ValidationError) as error:
        raise ConfigError("Could not save configuration") from error
