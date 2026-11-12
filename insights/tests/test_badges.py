"""Unit tests for session / subagent badge rules."""

from __future__ import annotations

from wdi.web import badges
from wdi.web.badges import (
    ERROR_PRONE,
    EXCELLENT,
    LEGEND,
    OVERSEARCHING,
    POOR,
    SLOW,
)


def _session(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "summary_score": None,
        "duration_ms": 1000,
        "calls": 0,
        "errors": 0,
        "search_count": 0,
        "edit_count": 0,
        "repeated_reads": 0,
    }
    base.update(over)
    return base


def test_excellent_and_poor_thresholds() -> None:
    assert EXCELLENT in badges.session_badges(_session(summary_score=80), None)
    assert POOR in badges.session_badges(_session(summary_score=40), None)
    mid = badges.session_badges(_session(summary_score=60), None)
    assert EXCELLENT not in mid and POOR not in mid


def test_slow_relative_to_average() -> None:
    assert SLOW in badges.session_badges(_session(duration_ms=200), avg_duration=100)
    assert SLOW not in badges.session_badges(_session(duration_ms=120), avg_duration=100)
    # No average (single/empty set) -> never slow.
    assert SLOW not in badges.session_badges(_session(duration_ms=10_000), avg_duration=None)


def test_error_prone() -> None:
    assert ERROR_PRONE in badges.session_badges(_session(calls=10, errors=3), None)
    assert ERROR_PRONE not in badges.session_badges(_session(calls=10, errors=1), None)
    assert ERROR_PRONE not in badges.session_badges(_session(calls=0, errors=0), None)


def test_oversearching_via_repeated_reads_or_search_heavy() -> None:
    assert OVERSEARCHING in badges.session_badges(_session(repeated_reads=1), None)
    assert OVERSEARCHING in badges.session_badges(_session(search_count=5, edit_count=1), None)
    assert OVERSEARCHING not in badges.session_badges(_session(search_count=5, edit_count=4), None)
    assert OVERSEARCHING not in badges.session_badges(_session(search_count=2, edit_count=0), None)


def test_subagent_badges() -> None:
    run = {
        "trace_calls": 10,
        "trace_errors": 3,
        "search_count": 5,
        "edit_file_count": 0,
        "repeated_reads": 0,
    }
    out = badges.subagent_badges(run)
    assert ERROR_PRONE in out and OVERSEARCHING in out
    clean = {
        "trace_calls": 5,
        "trace_errors": 0,
        "search_count": 0,
        "edit_file_count": 2,
        "repeated_reads": 0,
    }
    assert badges.subagent_badges(clean) == []


def test_legend_covers_every_emittable_glyph() -> None:
    emittable = {EXCELLENT, POOR, SLOW, ERROR_PRONE, OVERSEARCHING}
    assert set(LEGEND) == emittable
