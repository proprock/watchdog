from wdi.config import ModelPricing
from wdi.context_economics import Scenario, compare_scenarios, estimate_prompt
from wdi.metrics.pricing import PricingResolver


def test_unknown_price_does_not_become_free():
    pricing = PricingResolver({})
    assert estimate_prompt("hello", "unknown", 600, pricing)["cost"] is None
    assert all(r.cost is None for r in compare_scenarios(Scenario(), "unknown", pricing))


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
