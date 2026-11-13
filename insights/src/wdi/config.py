"""Loading, validation and creation of the wdi YAML configuration."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from agent_watchdog.readapi import default_data_dir

DEFAULT_CONFIG_PATH = Path("~/.wdi/config.yaml")


# USD per million tokens. Session logs carry no cost data, so prices are
# estimated from this table; users override it in the config.
def _default_pricing() -> dict[str, ModelPricing]:
    from wdi.metrics.pricing import Tariffs

    return {
        model: ModelPricing.model_validate(rates)
        for model, rates in Tariffs.default()._sections[0][1].items()
    }


class ConfigError(Exception):
    """Configuration loading or validation error."""


class SourceConfig(BaseModel):
    enabled: bool = True
    root: Path

    @field_validator("root")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class SourcesConfig(BaseModel):
    claude_code: SourceConfig = Field(
        default_factory=lambda: SourceConfig(root=Path("~/.claude/projects"))
    )
    codex: SourceConfig = Field(
        default_factory=lambda: SourceConfig(root=Path("~/.codex/sessions"))
    )


class ProjectsConfig(BaseModel):
    # canonical_name -> names/paths merged into one project across machines
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class WatchdogConfig(BaseModel):
    data_dirs: list[Path] = Field(default_factory=lambda: [default_data_dir()])

    @field_validator("data_dirs")
    @classmethod
    def _expand_data_dirs(cls, paths: list[Path]) -> list[Path]:
        return list(dict.fromkeys(path.expanduser().absolute() for path in paths))


class CoachModelsConfig(BaseModel):
    claude: list[str] = Field(default_factory=lambda: ["haiku", "sonnet", "opus"], min_length=1)
    codex: list[str] = Field(
        default_factory=lambda: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"], min_length=1
    )

    @field_validator("claude", "codex")
    @classmethod
    def nonempty_models(cls, models: list[str]) -> list[str]:
        if any(not model.strip() for model in models):
            raise ValueError("Coach model names must not be empty")
        return list(dict.fromkeys(model.strip() for model in models))


class LlmAnalysisConfig(BaseModel):
    enabled: bool = True
    backend: Literal["claude", "codex", "claude-cli"] = "claude"
    model: str = "haiku"
    # per-analyzer override, e.g. {"agent_efficiency": "sonnet"}
    models: dict[str, str] = Field(default_factory=dict)
    max_sessions_per_run: int = 10
    coach_models: CoachModelsConfig = Field(default_factory=CoachModelsConfig)
    codex_executable: str = "codex"

    def model_for(self, analysis_type: str) -> str:
        return self.models.get(analysis_type, self.model)

    def analysis_model_for(self, analysis_type: str) -> str:
        if self.backend == "codex" and self.model == "haiku":
            return self.models.get(analysis_type, self.coach_models.codex[0])
        return self.model_for(analysis_type)


class CostEstimationConfig(BaseModel):
    enabled: bool = True


class FeaturesConfig(BaseModel):
    llm_analysis: LlmAnalysisConfig = Field(default_factory=LlmAnalysisConfig)
    cost_estimation: CostEstimationConfig = Field(default_factory=CostEstimationConfig)


class ModelPricing(BaseModel):
    input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)
    cache_read: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    cache_write: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    cache_write_5m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_1h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    batch_input: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    batch_output: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ReportConfig(BaseModel):
    out_dir: Path = Path("./reports")

    @field_validator("out_dir")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class SyncConfig(BaseModel):
    export_dir: Path | None = None

    @field_validator("export_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class Settings(BaseModel):
    machine_id: str
    db_path: Path = Field(default=Path("~/.wdi/wdi.sqlite"), validate_default=True)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    pricing: dict[str, ModelPricing] = Field(default_factory=_default_pricing)
    tariffs: Path | None = None

    @field_validator("tariffs")
    @classmethod
    def _expand_tariffs(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    report: ReportConfig = Field(default_factory=ReportConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("db_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().absolute()


def load_settings(path: Path | None = None) -> Settings:
    """Read settings from YAML; missing file or invalid YAML raises ConfigError."""
    cfg_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(f"Config not found: {cfg_path}. Run 'wdi config init'.")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {cfg_path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping, got: {type(data).__name__}")
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config {cfg_path}:\n{exc}") from exc


def create_default_config(path: Path | None = None, *, force: bool = False) -> Settings:
    """Create the default config with a fresh machine_id and write it to disk."""
    cfg_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if cfg_path.exists() and not force:
        raise ConfigError(f"Config already exists: {cfg_path}. Use --force to overwrite.")
    settings = Settings(machine_id=str(uuid.uuid4()))
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    cfg_path.write_text(dumped, encoding="utf-8")
    return settings
