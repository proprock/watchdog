"""Evidence bundles: redacted excerpts, token estimates, and budget fitting.

A bundle leaves the machine when it is sent to a model, so it is an export
boundary (WD-115): every excerpt passes the credential filter here.
"""

import json
import math
from dataclasses import dataclass
from typing import Any

from agent_watchdog import privacy

# Measured with `claude -p` (docs/verification.md, WD-123): a real errors bundle of
# UUIDs, paths and escaped JSON cost 1.98 bytes/token on Sonnet 5; synthetic ASCII JSON
# 2.27 (Sonnet 5) and 2.66 (Haiku 4.5); UTF-8 Cyrillic about 3.6. Staying below the
# densest measurement keeps a fitted bundle inside its budget.
BYTES_PER_TOKEN = 1.9
EXCERPT_LIMIT = 16 * 1024
EVIDENCE_KEYS = frozenset(
    {"evidence_id", "failed_evidence_id", "cluster_id", "item_id", "event_ids"}
)


@dataclass(frozen=True)
class Draft:
    """Deterministic evidence before fitting: items are ranked, most important first."""

    facts: dict[str, Any]
    coverage: dict[str, Any]
    items: list[dict[str, Any]]


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN)


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def excerpt(value: object, limit: int = EXCERPT_LIMIT) -> str | None:
    """Return redacted text, keeping head and tail when it exceeds ``limit`` bytes."""
    if value is None:
        return None
    text = (
        value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    )
    text = privacy.text(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    head = encoded[: limit // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(limit // 2) :].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    return f"{head}\n[... {omitted} bytes omitted ...]\n{tail}"


def fit(draft: Draft, *, mode: str, window: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Keep facts and coverage, then add ranked items until the token budget is spent."""
    bundle: dict[str, Any] = {
        "mode": mode,
        "window": window,
        "facts": draft.facts,
        "coverage": draft.coverage | {"truncated": {"items": 0, "bytes": 0}},
        "items": [],
    }
    # Size the frame with the largest truncation record it can carry.
    frame = bundle | {"coverage": draft.coverage | {"truncated": {"items": 10**9, "bytes": 10**12}}}
    used = len(dumps(frame).encode("utf-8"))
    budget = max_tokens * BYTES_PER_TOKEN
    dropped = dropped_bytes = 0
    for item in draft.items:
        size = len(dumps(item).encode("utf-8")) + 2
        if dropped == 0 and used + size <= budget:
            bundle["items"].append(item)
            used += size
        else:
            dropped += 1
            dropped_bytes += size
    bundle["coverage"]["truncated"] = {"items": dropped, "bytes": dropped_bytes}
    return bundle


def evidence_ids(value: object) -> set[str]:
    """Collect every identifier a model may cite from a fitted bundle."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in EVIDENCE_KEYS and isinstance(item, str):
                found.add(item)
            elif key in EVIDENCE_KEYS and isinstance(item, list):
                found.update(entry for entry in item if isinstance(entry, str))
            else:
                found |= evidence_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= evidence_ids(item)
    return found
