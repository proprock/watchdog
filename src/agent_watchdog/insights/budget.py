"""Size the evidence bundle from the context window a model reported on its last call.

`claude -p` reports a model's window and answer limit only in its result, so the
first call for a model uses a fixed default and every later call scales with the
remembered window. The memory is a small per-user file of provider-reported
numbers, never session data; a missing or unreadable file falls back to the default.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_watchdog.config import UserPaths
from agent_watchdog.files import atomic_write

# Fits a 200K-token window with room for the system prompt, schema, answer and reasoning.
DEFAULT_MAX_BUNDLE_TOKENS = 120_000
# Leave a fifth of a large window unused: long inputs cost latency and answer quality,
# and the byte-based token estimate is approximate.
WINDOW_SHARE = 0.8
PROMPT_RESERVE = 8_000
ANSWER_RESERVE = 32_000


def memory_path(paths: UserPaths) -> Path:
    return paths.data / "insights" / "model-windows.json"


def _memory(paths: UserPaths) -> dict[str, Any]:
    try:
        value = json.loads(memory_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = value.get("models") if isinstance(value, dict) else None
    return models if isinstance(models, dict) else {}


def plan(paths: UserPaths, model: str, explicit: int | None) -> dict[str, Any]:
    """Return the bundle budget and where it came from."""
    if explicit is not None:
        return {"max_bundle_tokens": explicit, "source": "explicit", "context_window": None}
    entry = _memory(paths).get(model)
    entry = entry if isinstance(entry, dict) else {}
    window = entry.get("context_window")
    if type(window) is not int or window <= 0:
        return {
            "max_bundle_tokens": DEFAULT_MAX_BUNDLE_TOKENS,
            "source": "default",
            "context_window": None,
        }
    answer = entry.get("max_output_tokens")
    answer = answer if type(answer) is int and answer > 0 else ANSWER_RESERVE
    tokens = min(int(window * WINDOW_SHARE), window - answer - PROMPT_RESERVE)
    return {
        "max_bundle_tokens": max(tokens, 1000),
        "source": "remembered_window",
        "context_window": window,
    }


def remember(paths: UserPaths, model: str, provenance: dict[str, Any], now: datetime) -> None:
    """Record the window a call reported; best effort, never a reason to fail the run."""
    window = provenance.get("context_window")
    if type(window) is not int or window <= 0:
        return
    models = _memory(paths)
    models[model] = {
        "context_window": window,
        "max_output_tokens": provenance.get("max_output_tokens"),
        "resolved_models": provenance.get("models"),
        "cli_version": provenance.get("version"),
        "observed_at": now.isoformat(),
    }
    document = {"schema_version": 1, "models": models}
    try:
        atomic_write(memory_path(paths), json.dumps(document, indent=2, sort_keys=True).encode())
    except OSError:
        return
