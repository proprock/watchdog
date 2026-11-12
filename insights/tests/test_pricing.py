"""Pricing configuration compatibility and cache estimates."""

import pytest
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
