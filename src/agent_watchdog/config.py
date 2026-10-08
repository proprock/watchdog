"""Versioned user configuration; no directories are created during reads."""

import json
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

import tomli_w
from platformdirs import PlatformDirs
from pydantic import Field, ValidationError, field_validator, model_validator

from agent_watchdog.files import atomic_write
from agent_watchdog.models import NonNegative, Positive, StrictModel, Versioned


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
    content_days: NonNegative = 30
    metrics_days: NonNegative = 180
    transcript_failure_minutes: Positive = 15
    project_bytes: Positive = 2 * 1024**3
    inbox_bytes: Positive = 64 * 1024**2
    payload_bytes: Positive = 1024**2
    log_files: Positive = 5
    log_bytes: Positive = 10 * 1024**2
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_detail: bool = False
    # Kill switch for every control action of the decision channel (WD-142):
    # false means the daemon answers "no opinion" to every hook call for the
    # project. Set it in [defaults] to switch the channel off globally. One rule
    # is turned off by name with `[rules] disabled` (`rules disable`).
    policy_intervene: bool = True
    # Kill switch for `insights`, the only path that sends a project's evidence to a
    # model. The explicit command is the opt-in; false refuses the call (a dry run,
    # which sends nothing, still works) for every project or just one.
    insights_llm_enabled: bool = True

    @model_validator(mode="after")
    def ordered_quotas(self) -> Self:
        if not self.payload_bytes <= self.inbox_bytes <= self.project_bytes:
            raise ValueError("Require payload_bytes <= inbox_bytes <= project_bytes")
        return self


class Overrides(StrictModel):
    capture_content: bool | None = None
    reserve_bytes: Positive | None = None
    content_days: NonNegative | None = None
    metrics_days: NonNegative | None = None
    transcript_failure_minutes: Positive | None = None
    project_bytes: Positive | None = None
    inbox_bytes: Positive | None = None
    payload_bytes: Positive | None = None
    policy_intervene: bool | None = None
    insights_llm_enabled: bool | None = None

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


def project_aliases(projects: Iterable[Project]) -> dict[UUID, str]:
    """Derive stable-in-config-order CLI aliases without changing stored identities."""
    aliases: dict[UUID, str] = {}
    used: set[str] = set()
    for project in projects:
        base = project.root.name or project.root.drive.removesuffix(":") or "project"
        alias = base
        suffix = 2
        while alias in used:
            alias = f"{base}-{suffix}"
            suffix += 1
        aliases[project.id] = alias
        used.add(alias)
    return aliases


def project_for_reference(projects: Iterable[Project], reference: str) -> Project | None:
    """Resolve a CLI alias first, retaining UUID input for compatibility."""
    registered = tuple(projects)
    aliases = project_aliases(registered)
    project = next((item for item in registered if aliases[item.id] == reference), None)
    if project is not None:
        return project
    try:
        project_id = UUID(reference)
    except ValueError:
        return None
    return next((item for item in registered if item.id == project_id), None)


class Pricing(StrictModel):
    """Optional default location of the list-price tariff file for `usage`."""

    tariffs: Path | None = None

    @field_validator("tariffs")
    @classmethod
    def absolute_tariffs(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("Tariff path must be absolute")
        return expanded


RuleName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Rules(StrictModel):
    """Which decision-channel rules run (WD-142); the rule bodies live in files.

    ``approved`` maps a user rule to the SHA-256 of the file the user approved: a
    changed file no longer matches and is never applied. ``disabled`` turns any
    rule off; ``enabled`` opts in a built-in that ships off. ``tiers`` ranks model
    families from lowest to highest for rules that move a model down a tier.
    """

    approved: dict[RuleName, Sha256] = Field(default_factory=dict)
    disabled: list[RuleName] = Field(default_factory=list)
    enabled: list[RuleName] = Field(default_factory=list)
    tiers: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]] = Field(
        default_factory=lambda: ["haiku", "sonnet", "opus", "fable"], min_length=1
    )

    @model_validator(mode="after")
    def unique_tiers(self) -> Self:
        if len(set(self.tiers)) != len(self.tiers):
            raise ValueError("Model tiers must be unique")
        return self


class Config(Versioned):
    defaults: Limits = Field(default_factory=Limits)
    projects: tuple[Project, ...] = ()
    auto_add_projects: bool = False
    pipeline_telemetry: bool = True
    pricing: Pricing = Field(default_factory=Pricing)
    rules: Rules = Field(default_factory=Rules)
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
