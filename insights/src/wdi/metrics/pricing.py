"""Cost estimation from the configured pricing table.

Session logs carry no cost, so cost is estimated from token counts and a
per-model price table. Model ids in the data are more specific than table keys
(e.g. claude-haiku-4-5-20251001 vs claude-haiku-4-5), so resolution is by the
longest matching key prefix.
"""

from __future__ import annotations

from wdi.config import ModelPricing

_MILLION = 1_000_000


class PricingResolver:
    def __init__(self, pricing: dict[str, ModelPricing]) -> None:
        # Longest keys first so the most specific prefix wins.
        self._pricing = sorted(pricing.items(), key=lambda kv: len(kv[0]), reverse=True)

    def resolve(self, model: str | None) -> ModelPricing | None:
        if not model:
            return None
        for key, prices in self._pricing:
            if model.startswith(key):
                return prices
        return None

    def cost(
        self,
        model: str | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float | None:
        prices = self.resolve(model)
        if prices is None:
            return None
        cache_write = (
            prices.cache_write_5m if prices.cache_write_5m is not None else prices.cache_write
        )
        return (
            input_tokens * prices.input
            + output_tokens * prices.output
            + cache_read_tokens * prices.cache_read
            + cache_creation_tokens * cache_write
        ) / _MILLION
