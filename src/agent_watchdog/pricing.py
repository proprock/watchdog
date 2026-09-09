"""List-price cost estimates for v6 token telemetry (WD-114).

These are list-price approximations from a user-supplied tariff file, **not
billed spend**: subscription and enterprise pricing differ, and Anthropic's 5m
and 1h cache-write tiers are collapsed into one ``cache_write`` rate by the v6
schema (see ``docs/storage.md``). Nothing here is persisted; an estimate is
recomputed from the raw counters on every call, so a tariff edit re-prices
history without a migration.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Tariff rate field -> the ``event_facts`` counter it prices. Reasoning/thinking
# tokens (a subset of ``output_tokens``) and Codex ``total_tokens`` (an
# overlapping sum) are deliberately absent: pricing them double-counts.
RATE_FIELDS: dict[str, str] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cached_input_tokens",
    "cache_write": "cache_write_input_tokens",
}
_MILLION = Decimal(1_000_000)
QUANTUM = Decimal("0.000001")


class PricingError(ValueError):
    """The tariff file is missing, unreadable, or internally invalid."""


@dataclass(frozen=True)
class Rate:
    """One model's rates from the tariff section effective at a moment."""

    effective: date
    model: str
    rates: Mapping[str, Decimal]  # field -> USD per 1e6 tokens; a missing field is unpriced


def _decimal(value: object, where: str) -> Decimal:
    if not isinstance(value, str):
        raise PricingError(f"{where}: rate must be a quoted decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise PricingError(f"{where}: {value!r} is not a decimal") from error
    if parsed < 0 or not parsed.is_finite():
        raise PricingError(f"{where}: rate must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class Tariffs:
    currency: str
    path: str
    # sorted ascending by effective date; each table is a complete rate list
    _sections: tuple[tuple[date, Mapping[str, Mapping[str, Decimal]]], ...]

    @classmethod
    def load(cls, path: str | Path) -> "Tariffs":
        source = Path(path)
        try:
            raw = tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise PricingError(f"Cannot read tariff file {source}") from error
        currency = raw.pop("currency", "USD")
        if not isinstance(currency, str) or not currency:
            raise PricingError("currency must be a non-empty string")
        sections: list[tuple[date, dict[str, dict[str, Decimal]]]] = []
        for key, table in raw.items():
            try:
                effective = date.fromisoformat(key)
            except ValueError as error:
                raise PricingError(f"Section {key!r} is not an ISO date") from error
            if not isinstance(table, dict) or not table:
                raise PricingError(f"Section {key!r} has no model rates")
            models: dict[str, dict[str, Decimal]] = {}
            for model, row in table.items():
                if not isinstance(row, dict) or not row:
                    raise PricingError(f"{key} / {model}: rates must be a table")
                unknown = set(row) - set(RATE_FIELDS)
                if unknown:
                    raise PricingError(f"{key} / {model}: unknown rate fields {sorted(unknown)}")
                models[model] = {
                    field: _decimal(row[field], f"{key} / {model} / {field}") for field in row
                }
            sections.append((effective, models))
        if not sections:
            raise PricingError("Tariff file has no dated sections")
        sections.sort(key=lambda item: item[0])
        return cls(currency=currency, path=str(source), _sections=tuple(sections))

    def rate(self, model: str | None, at: datetime | None) -> Rate | None:
        """The rate for ``model`` under the latest section effective at ``at``.

        ``None`` when ``at`` precedes every section (or model/at is missing). A
        ``Rate`` with empty ``rates`` when a section applies but omits the model.
        """
        if not model or at is None:
            return None
        when = at.date()
        applicable: tuple[date, Mapping[str, Mapping[str, Decimal]]] | None = None
        for effective, table in self._sections:
            if when >= effective:
                applicable = (effective, table)
            else:
                break
        if applicable is None:
            return None
        effective, table = applicable
        return Rate(effective, model, table.get(model, {}))


def estimate(counts: Mapping[str, int | None], rate: Rate) -> tuple[Decimal, dict[str, object]]:
    """Return ``(cost_estimate, provenance)`` for one usage row's raw counters."""
    cost = Decimal(0)
    by_field: dict[str, Decimal] = {}
    unpriced_tokens = 0
    missing_field = False
    for field, counter in RATE_FIELDS.items():
        tokens = counts.get(counter)
        if not isinstance(tokens, int) or tokens <= 0:
            continue
        unit = rate.rates.get(field)
        if unit is None:
            unpriced_tokens += tokens
            missing_field = True
            continue
        line = Decimal(tokens) * unit / _MILLION
        cost += line
        by_field[field] = line
    provenance: dict[str, object] = {
        "tariff_date": rate.effective.isoformat(),
        "priced_fields": list(by_field),
        "by_field": by_field,
        "unpriced_tokens": unpriced_tokens,
    }
    if not rate.rates:
        provenance["unpriced_reason"] = "model_not_in_tariff"
    elif missing_field:
        provenance["unpriced_reason"] = "rate_field_missing"
    return cost.quantize(QUANTUM), provenance
