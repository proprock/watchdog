"""Pricing configuration compatibility and cache estimates."""

import pytest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from wdi.config import Settings
from wdi.metrics import pricing as engine
from wdi.config import ModelPricing
from wdi.metrics.pricing import PricingResolver


@pytest.mark.parametrize("cache_write_5m, expected", [(None, 7.0), (6.25, 6.25), (0.0, 0.0)])
def test_cache_write_rate_prefers_explicit_5m(
    cache_write_5m: float | None, expected: float
) -> None:
    rates = ModelPricing.model_validate(
        {"input": 5.0, "output": 25.0, "cache_write": 7.0, "cache_write_5m": cache_write_5m}
    )
    pricing = PricingResolver({"claude-opus-5": rates})
    assert pricing.cost("claude-opus-5", cache_creation_tokens=1_000_000) == expected


def test_reserved_rates_do_not_discount_standard_sessions() -> None:
    rates = ModelPricing.model_validate(
        {
            "input": 5.0,
            "output": 25.0,
            "cache_read": 0.5,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10.0,
            "batch_input": 2.5,
            "batch_output": 12.5,
        }
    )
    pricing = PricingResolver({"claude-opus-5": rates})
    assert pricing.cost(
        "claude-opus-5-20260903",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    ) == pytest.approx(36.75)


def _tariffs(tmp_path: Path, text: str):
    path = tmp_path / "tariffs.toml"
    path.write_text(text, encoding="utf-8")
    return engine.Tariffs.load(path)


def test_dated_prefix_selection_reprices_history(tmp_path: Path) -> None:
    tariffs = _tariffs(
        tmp_path,
        """
["2026-01-01"]
"m" = {input="1"}
"m-special" = {input="2"}
["2026-07-01"]
"m" = {input="3"}
""",
    )
    early = tariffs.rate("m-special-version", datetime(2026, 1, 1, tzinfo=UTC))
    late = tariffs.rate("m-special-version", datetime(2026, 7, 1, tzinfo=UTC))
    assert early is not None and late is not None
    assert early.effective == date(2026, 1, 1)
    counts = {"input_tokens": 1_000_000}
    assert engine.estimate(counts, early, provider="claude")[0] == Decimal("2.000000")
    assert engine.estimate(counts, late, provider="claude")[0] == Decimal("3.000000")
    assert tariffs.rate("m", datetime(2025, 1, 1, tzinfo=UTC)) is None
    absent = tariffs.rate("unknown", datetime(2026, 1, 1, tzinfo=UTC))
    assert absent is not None
    _, provenance = engine.estimate(counts, absent, provider="claude")
    assert provenance["unpriced_reason"] == "model_not_in_tariff"


@pytest.mark.parametrize(
    "row",
    [
        "input=1",
        'input="-1"',
        'input="NaN"',
        'input="Infinity"',
        'input="invalid"',
        'surprise="1"',
    ],
)
def test_invalid_tariff_rates_are_rejected(tmp_path: Path, row: str) -> None:
    with pytest.raises(engine.PricingError):
        _tariffs(tmp_path, f'["2026-01-01"]\n"m" = {{{row}}}\n')


@pytest.mark.parametrize(
    "text", ["", '["bad-date"]\n"m"={input="1"}', '["2026-01-01"]', "currency=123"]
)
def test_invalid_tariff_tables_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(engine.PricingError):
        _tariffs(tmp_path, text)


def test_missing_tariff_file_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(engine.PricingError, match="Cannot read tariff"):
        engine.Tariffs.load(tmp_path / "absent.toml")


def test_decimal_estimate_preserves_partial_pricing_and_missing_cache() -> None:
    rate = engine.Rate(
        date(2026, 1, 1), "m", {"input": Decimal("0.123456789"), "output": Decimal("2")}
    )
    counts = {"input_tokens": 1_000_000, "output_tokens": 1, "cached_input_tokens": 5}
    cost, provenance = engine.estimate(counts, rate, provider="claude")
    assert cost == Decimal("0.123459")
    assert provenance["by_field"]["input"] == Decimal("0.123456789")
    assert provenance["unpriced_tokens"] == 5
    assert provenance["unpriced_reason"] == "rate_field_missing"
    cost, provenance = engine.estimate(
        {"input_tokens": 1_000_000, "output_tokens": 1}, rate, provider="codex"
    )
    assert cost == Decimal("0.000002")
    assert provenance["unpriced_tokens"] == 1_000_000
    assert provenance["unpriced_reason"] == "cached_input_unavailable"


@pytest.mark.parametrize(
    "tier, expected", [(None, "7.000000"), ("6.25", "6.250000"), ("0", "0.000000")]
)
def test_decimal_cache_write_5m_fallback(tier: str | None, expected: str) -> None:
    rates = {"cache_write": Decimal("7")}
    if tier is not None:
        rates["cache_write_5m"] = Decimal(tier)
    cost, provenance = engine.estimate(
        {"cache_write_input_tokens": 1_000_000},
        engine.Rate(date(2026, 1, 1), "m", rates),
        provider="claude",
    )
    assert cost == Decimal(expected)
    assert provenance["priced_tokens"] == {"cache_write": 1_000_000}


def test_resolver_provider_convention_is_explicit_and_net_by_default(tmp_path: Path) -> None:
    tariffs = _tariffs(tmp_path, '["2026-01-01"]\n"m"={input="2",cache_read="1"}\n')
    resolver = PricingResolver(tariffs)
    at = datetime(2026, 2, 1, tzinfo=UTC)
    assert (
        resolver.cost(
            "m", input_tokens=1_000_000, cache_read_tokens=500_000, at=at, provider="codex"
        )
        == 2.5
    )
    assert (
        resolver.cost(
            "m",
            input_tokens=1_000_000,
            cache_read_tokens=500_000,
            at=at,
            provider="codex",
            input_is_net=False,
        )
        == 1.5
    )
    assert resolver.cost("m", input_tokens=1, at=datetime(2025, 1, 1, tzinfo=UTC)) is None
    assert resolver.cost("unknown", input_tokens=1, at=at) is None


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-opus-4-8", 32.625),
        ("claude-sonnet-4-6", 6.525),
        ("claude-opus-5", 10.875),
        ("claude-opus-4-6", 10.875),
        ("claude-sonnet-5", 4.35),
        ("claude-haiku-4-5", 2.175),
        ("gpt-5", 3.3125),
    ],
)
def test_packaged_defaults_match_frozen_old_vector(model: str, expected: float) -> None:
    # Frozen old output: 1M input, 200k output, 500k read, 100k creation.
    resolver = PricingResolver.from_settings(Settings(machine_id="fixture"))
    assert (
        resolver.cost(
            model,
            input_tokens=1_000_000,
            output_tokens=200_000,
            cache_read_tokens=500_000,
            cache_creation_tokens=100_000,
        )
        == expected
    )
