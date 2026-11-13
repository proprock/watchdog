from wdi.config import ModelPricing
from wdi.context_economics import Scenario, compare_scenarios, estimate_prompt
from wdi.metrics.pricing import PricingResolver, Tariffs


def test_unknown_price_does_not_become_free():
    pricing = PricingResolver({})
    assert estimate_prompt("hello", "unknown", 600, pricing)["cost"] is None
    assert all(r.cost is None for r in compare_scenarios(Scenario(), "unknown", pricing))


def test_partial_tariff_prices_known_prompt_without_inventing_output_rate(tmp_path):
    path = tmp_path / "partial.toml"
    path.write_text('["2026-01-01"]\nmodel={input="2"}\n', encoding="utf-8")
    pricing = PricingResolver(Tariffs.load(path))

    known = estimate_prompt("12345678", "model", 0, pricing)
    assert known["input_tokens"] == 2
    assert known["cost"] == 0.000004
    assert known["rates"] is None
    missing = estimate_prompt("12345678", "model", 1, pricing)
    assert missing["cost"] is None
    assert missing["rates"] is None


def test_tariff_missing_cache_fields_does_not_present_free_cache_rates(tmp_path):
    path = tmp_path / "partial.toml"
    path.write_text('["2026-01-01"]\nmodel={input="2",output="3"}\n', encoding="utf-8")
    pricing = PricingResolver(Tariffs.load(path))

    result = estimate_prompt("12345678", "model", 1, pricing)
    assert result["cost"] == 0.000007
    assert result["rates"] is None


def test_compaction_includes_overhead_and_can_fail_to_pay_back():
    pricing = PricingResolver({"test": ModelPricing(input=10, output=30, cache_read=1)})
    rows = compare_scenarios(
        Scenario(calls=1, context=10000, remove_fraction=0.1, summary_tokens=5000), "test", pricing
    )
    assert rows[1].cost is not None and rows[0].cost is not None
    assert rows[1].cost > rows[0].cost
    assert rows[1].break_even_calls is None


def test_preventing_output_saves_reads_without_compaction_charge():
    pricing = PricingResolver({"test": ModelPricing(input=10, output=30, cache_read=1)})
    rows = compare_scenarios(
        Scenario(calls=20, context=10000, removable_tokens=2000), "test", pricing
    )
    assert rows[2].cost is not None and rows[0].cost is not None
    assert rows[2].cost < rows[0].cost
    assert rows[2].saving is not None
    assert rows[2].saving > 0


def test_zero_removal_never_claims_compaction_savings():
    pricing = PricingResolver({"test": ModelPricing(input=10, output=30, cache_read=1)})
    rows = compare_scenarios(Scenario(remove_fraction=0, summary_tokens=1000), "test", pricing)
    assert rows[1].saving is not None
    assert rows[1].saving < 0
