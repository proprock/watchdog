"""Unit tests for presentation helpers."""

from __future__ import annotations

from wdi.cli_format import display_name


def test_display_name_prefers_title() -> None:
    row = {"title": "Fix auth bug", "first_prompt": "help me", "session_id": "abcdef123456"}
    assert display_name(row) == "Fix auth bug"


def test_display_name_falls_back_to_first_prompt() -> None:
    row = {"title": None, "first_prompt": "  refactor the\n  parser  ", "session_id": "abcdef12"}
    assert display_name(row) == "refactor the parser"


def test_display_name_truncates_long_prompt() -> None:
    prompt = "x" * 100
    row = {"title": "", "first_prompt": prompt, "session_id": "abcdef12"}
    out = display_name(row)
    assert out.endswith("…")
    assert len(out) == 61  # 60 chars + ellipsis


def test_display_name_falls_back_to_short_id() -> None:
    row = {"title": None, "first_prompt": None, "session_id": "abcdef123456"}
    assert display_name(row) == "abcdef12"


def test_display_name_handles_missing_keys() -> None:
    assert display_name({"session_id": "abcdef123456"}) == "abcdef12"
    assert display_name({}) == "—"
