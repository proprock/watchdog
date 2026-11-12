"""Small presentation helpers for terminal output."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

DASH = "—"

_NAME_MAX = 60


def display_name(row: sqlite3.Row | Mapping[str, object]) -> str:
    """Human label for a session: title -> first prompt snippet -> short id.

    Uses the native agent-provided title when present, then falls back to a
    snippet of the first user prompt, then the truncated session id.
    """
    keys = row.keys()
    title = row["title"] if "title" in keys else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    prompt = row["first_prompt"] if "first_prompt" in keys else None
    if isinstance(prompt, str) and prompt.strip():
        snippet = " ".join(prompt.split())
        return snippet[:_NAME_MAX] + ("…" if len(snippet) > _NAME_MAX else "")
    sid = row["session_id"] if "session_id" in keys else None
    return str(sid)[:8] if sid else DASH


def fmt_int(value: int | None) -> str:
    return f"{value:,}" if value is not None else DASH


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return DASH
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def fmt_cost(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else DASH


def fmt_pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else DASH


def fmt_duration(ms: int | None) -> str:
    if not ms:
        return DASH
    seconds = ms // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def fmt_ratio(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else DASH
