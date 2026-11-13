"""Tests for configuration loading and creation."""

import uuid
from pathlib import Path

import pytest
import yaml
from wdi.config import (
    ConfigError,
    LlmAnalysisConfig,
    Settings,
    create_default_config,
    load_settings,
)


def test_create_default_config_writes_file_with_machine_id(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"

    settings = create_default_config(cfg_path)

    assert cfg_path.is_file()
    uuid.UUID(settings.machine_id)
    loaded = load_settings(cfg_path)
    assert loaded == settings


def test_create_default_config_refuses_overwrite(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    first = create_default_config(cfg_path)

    with pytest.raises(ConfigError):
        create_default_config(cfg_path)

    second = create_default_config(cfg_path, force=True)
    assert second.machine_id != first.machine_id


def test_load_settings_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config init"):
        load_settings(tmp_path / "nope.yaml")


def test_load_settings_invalid_yaml_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("machine_id: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_settings(cfg_path)


def test_load_settings_non_mapping_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_settings(cfg_path)


def test_partial_config_fills_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"machine_id: {uuid.uuid4()}\n", encoding="utf-8")

    settings = load_settings(cfg_path)

    assert settings.sources.claude_code.enabled
    assert settings.sources.claude_code.root.name == "projects"
    assert settings.sources.codex.root.name == "sessions"
    assert settings.features.llm_analysis.model == "haiku"
    assert settings.features.cost_estimation.enabled
    assert settings.pricing


def test_paths_are_expanded(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"machine_id: {uuid.uuid4()}\ndb_path: ~/custom/wdi.sqlite\n",
        encoding="utf-8",
    )

    settings = load_settings(cfg_path)

    assert "~" not in str(settings.db_path)
    assert settings.db_path.is_absolute()


def test_llm_model_per_analyzer_with_fallback() -> None:
    settings = Settings(machine_id=str(uuid.uuid4()))
    llm = settings.features.llm_analysis
    llm.models["agent_efficiency"] = "sonnet"

    assert llm.model_for("agent_efficiency") == "sonnet"
    assert llm.model_for("prompt_quality") == "haiku"


def test_codex_analysis_uses_codex_default_instead_of_haiku() -> None:
    llm = LlmAnalysisConfig(backend="codex", models={"prompt_quality": "fixture-override"})
    assert llm.analysis_model_for("session_summary") == llm.coach_models.codex[0]
    assert llm.analysis_model_for("prompt_quality") == "fixture-override"


def test_load_settings_rejects_unknown_analysis_backend(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("features: {llm_analysis: {backend: typo}}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"llm_analysis.backend"):
        load_settings(cfg)


def test_extended_pricing_survives_config_round_trip(tmp_path: Path) -> None:
    rates = {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "batch_input": 2.5,
        "batch_output": 12.5,
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"machine_id": str(uuid.uuid4()), "pricing": {"claude-opus-5": rates}}),
        encoding="utf-8",
    )
    settings = load_settings(path)
    serialized = settings.model_dump(mode="json")
    assert serialized["pricing"]["claude-opus-5"] == {"cache_write": 0.0, **rates}
    path.write_text(yaml.safe_dump(serialized), encoding="utf-8")
    assert load_settings(path) == settings


def test_tariffs_override_inline_and_packaged_rates_completely(tmp_path: Path) -> None:
    from wdi.metrics.pricing import PricingResolver

    tariffs = tmp_path / "tariffs.toml"
    tariffs.write_text('["2026-01-01"]\n"custom"={input="9",output="0"}\n', encoding="utf-8")
    settings = Settings.model_validate(
        {
            "machine_id": "fixture",
            "tariffs": str(tariffs),
            "pricing": {"inline": {"input": 2, "output": 3}},
        }
    )
    assert settings.tariffs == tariffs
    resolver = PricingResolver.from_settings(settings)
    assert resolver.cost("custom", input_tokens=1_000_000) == 9
    assert resolver.cost("inline", input_tokens=1) is None
    assert resolver.cost("gpt-5", input_tokens=1) is None


def test_inline_prices_normalize_without_default_model_fallback() -> None:
    from wdi.metrics.pricing import PricingResolver

    settings = Settings.model_validate(
        {"machine_id": "fixture", "pricing": {"custom": {"input": 2, "output": 3}}}
    )
    resolver = PricingResolver.from_settings(settings)
    assert resolver.cost("custom-version", input_tokens=1_000_000) == 2
    assert resolver.cost("gpt-5", input_tokens=1) is None


def test_tariff_setting_expands_home() -> None:
    settings = Settings.model_validate({"machine_id": "fixture", "tariffs": "~/rates.toml"})
    assert settings.tariffs == Path("~/rates.toml").expanduser()


@pytest.mark.parametrize("rate", [-1, float("nan"), float("inf")])
def test_inline_pricing_rejects_invalid_rates(rate: float) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"machine_id": "fixture", "pricing": {"custom": {"input": rate, "output": 1}}}
        )


@pytest.mark.parametrize("models", [[], [" "]])
def test_coach_model_menu_requires_nonempty_names(tmp_path: Path, models: list[str]) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "machine_id": str(uuid.uuid4()),
                "features": {"llm_analysis": {"coach_models": {"codex": models}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(path)
