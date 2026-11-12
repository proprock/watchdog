"""Unicode badges flagging notable sessions and subagent runs.

Pure functions over query rows. The same definitions drive the rendered badges
and the on-page legend, so the two never drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

GOOD_SCORE = 80
POOR_SCORE = 40
SLOW_FACTOR = 1.5
ERROR_RATE = 0.2
SEARCH_MIN = 3


@dataclass(frozen=True)
class Badge:
    icon: str
    label: str


EXCELLENT = Badge("⭐", "Excellent — LLM score ≥ 80")
POOR = Badge("⚠️", "Poor — LLM score ≤ 40")
SLOW = Badge("🐢", "Slow — duration well above average")
ERROR_PRONE = Badge("🔥", "Error-prone — high tool error rate")
OVERSEARCHING = Badge("🔁", "Oversearching — search-heavy / redundant reads")

# Order defines render + legend order.
LEGEND: list[Badge] = [EXCELLENT, POOR, SLOW, ERROR_PRONE, OVERSEARCHING]


def _num(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    return float(value) if isinstance(value, int | float) else 0.0


def _opt(row: Mapping[str, object], key: str) -> float | None:
    value = row.get(key)
    return float(value) if isinstance(value, int | float) else None


def _oversearching(search: float, edit: float, repeated: float) -> bool:
    return repeated > 0 or (search >= SEARCH_MIN and search > 2 * edit)


def session_badges(row: Mapping[str, object], avg_duration: float | None) -> list[Badge]:
    badges: list[Badge] = []
    score = _opt(row, "summary_score")
    if score is not None and score >= GOOD_SCORE:
        badges.append(EXCELLENT)
    elif score is not None and score <= POOR_SCORE:
        badges.append(POOR)

    duration = _opt(row, "duration_ms")
    if duration is not None and avg_duration and duration > SLOW_FACTOR * avg_duration:
        badges.append(SLOW)

    calls = _num(row, "calls")
    if calls > 0 and _num(row, "errors") / calls > ERROR_RATE:
        badges.append(ERROR_PRONE)

    if _oversearching(
        _num(row, "search_count"), _num(row, "edit_count"), _num(row, "repeated_reads")
    ):
        badges.append(OVERSEARCHING)
    return badges


def subagent_badges(row: Mapping[str, object]) -> list[Badge]:
    badges: list[Badge] = []
    calls = _num(row, "trace_calls")
    if calls > 0 and _num(row, "trace_errors") / calls > ERROR_RATE:
        badges.append(ERROR_PRONE)
    if _oversearching(
        _num(row, "search_count"), _num(row, "edit_file_count"), _num(row, "repeated_reads")
    ):
        badges.append(OVERSEARCHING)
    return badges
