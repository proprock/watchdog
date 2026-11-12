"""Explicit counterfactuals; API equivalents are not subscription quota forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Literal

from pydantic import BaseModel, Field

from wdi.llm.service import _estimate_tokens
from wdi.metrics.pricing import PricingResolver


class Scenario(BaseModel):
    model: str = ""
    provider: Literal["codex", "claude"] = "codex"
    calls: int = Field(default=20, ge=1, le=1000)
    context: int = Field(default=100000, ge=0, le=2000000)
    cache_hit: float = Field(default=0.9, ge=0, le=1)
    remove_fraction: float = Field(default=0.3, ge=0, le=1)
    preserved_prefix: int = Field(default=10000, ge=0, le=2000000)
    summary_tokens: int = Field(default=2000, ge=0, le=100000)
    recovery_tokens: int = Field(default=2000, ge=0, le=2000000)
    removable_tokens: int = Field(default=10000, ge=0, le=2000000)
    avoided_calls: int = Field(default=1, ge=0, le=1000)
    output_per_call: int = Field(default=500, ge=0, le=100000)


@dataclass
class ScenarioResult:
    name: str
    input_tokens: int
    output_tokens: int
    cost: float | None
    saving: float | None = None
    break_even_calls: int | None = None


def estimate_prompt(
    prompt: str, model: str, output_budget: int, pricing: PricingResolver
) -> dict[str, object]:
    tokens = _estimate_tokens(prompt)
    return {
        "input_tokens": tokens,
        "output_tokens": output_budget,
        "cost": pricing.cost(model, input_tokens=tokens, output_tokens=output_budget),
        "rates": pricing.resolve(model),
        "model": model,
        "method": (
            "Approximately 4 characters/token; CLI overhead is not observable. "
            "Output is an estimate, not an enforced limit."
        ),
    }


def compare_scenarios(s: Scenario, model: str, pricing: PricingResolver) -> list[ScenarioResult]:
    def cost(tokens: float, output: float = 0, hit: float | None = None) -> float | None:
        fraction = s.cache_hit if hit is None else hit
        return pricing.cost(
            model,
            input_tokens=round(tokens * (1 - fraction)) if s.provider == "codex" else 0,
            cache_creation_tokens=round(tokens * (1 - fraction)) if s.provider == "claude" else 0,
            cache_read_tokens=round(tokens * fraction),
            output_tokens=round(output),
        )

    baseline = cost(s.calls * s.context, s.calls * s.output_per_call)
    retained = max(min(s.preserved_prefix, s.context), round(s.context * (1 - s.remove_fraction)))
    after = retained + s.summary_tokens + s.recovery_tokens
    compact_charge = cost(s.context, s.summary_tokens)
    warmup = cost(after - min(s.preserved_prefix, retained), hit=0)
    prefix_charge = cost(min(s.preserved_prefix, retained), hit=1)
    later = cost((s.calls - 1) * after, s.calls * s.output_per_call)
    compact = (
        sum(v for v in (compact_charge, warmup, prefix_charge, later) if v is not None)
        if baseline is not None
        else None
    )
    prevented = min(s.removable_tokens, s.context)
    calls = max(0, s.calls - s.avoided_calls)
    rows = [
        ScenarioResult(
            "Continue unchanged", s.calls * s.context, s.calls * s.output_per_call, baseline
        ),
        ScenarioResult(
            "Earlier native compact",
            s.context + s.calls * after,
            s.calls * s.output_per_call + s.summary_tokens,
            compact,
        ),
        ScenarioResult(
            "Prevent output before ingestion",
            s.calls * (s.context - prevented),
            s.calls * s.output_per_call,
            cost(s.calls * (s.context - prevented), s.calls * s.output_per_call),
        ),
        ScenarioResult(
            "Avoid retry calls",
            calls * s.context,
            calls * s.output_per_call,
            cost(calls * s.context, calls * s.output_per_call),
        ),
    ]
    for row in rows:
        row.saving = baseline - row.cost if baseline is not None and row.cost is not None else None
    per_call = cost(s.context - after)
    if (
        per_call is not None
        and per_call > 0
        and compact_charge is not None
        and warmup is not None
        and prefix_charge is not None
    ):
        overhead = compact_charge + warmup + prefix_charge - (cost(after) or 0)
        rows[1].break_even_calls = max(1, ceil(overhead / per_call))
    return rows
