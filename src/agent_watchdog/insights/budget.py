"""Size the evidence bundle from the context window a model reported on its last call.

`claude -p` reports a model's window and answer limit only in its result, so the
first call for a model uses a fixed default and every later call scales with the
remembered window. The memory is a small per-user file of provider-reported
numbers keyed by backend and model; legacy bare model entries are read as Claude
windows. It never contains session data.
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
        return {"models": {}, "backends": {}}
    models = value.get("models") if isinstance(value, dict) else None
    backends = value.get("backends") if isinstance(value, dict) else None
    return {
        "models": models if isinstance(models, dict) else {},
        "backends": backends if isinstance(backends, dict) else {},
    }


def plan(
    paths: UserPaths, model: str, explicit: int | None, *, backend: str = "claude"
) -> dict[str, Any]:
    """Return the bundle budget and where it came from."""
    if explicit is not None:
        return {"max_bundle_tokens": explicit, "source": "explicit", "context_window": None}
    memory = _memory(paths)
    backend_models = memory["backends"].get(backend)
    entry = backend_models.get(model) if isinstance(backend_models, dict) else None
    if entry is None and backend == "claude":
        entry = memory["models"].get(model)
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


def reported_windows(paths: UserPaths) -> dict[str, int]:
    """Context windows the Claude CLI reported, keyed by resolved model id."""
    windows: dict[str, int] = {}
    memory = _memory(paths)
    entries = list(memory["models"].values())
    claude_models = memory["backends"].get("claude")
    if isinstance(claude_models, dict):
        entries.extend(claude_models.values())
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("context_window")) is not int:
            continue
        for name in entry.get("resolved_models") or []:
            if isinstance(name, str):
                windows[name] = entry["context_window"]
    return windows


def remember(
    paths: UserPaths,
    model: str,
    provenance: dict[str, Any],
    now: datetime,
    *,
    backend: str = "claude",
) -> None:
    """Record the window a call reported; best effort, never a reason to fail the run."""
    window = provenance.get("context_window")
    if type(window) is not int or window <= 0:
        return
    memory = _memory(paths)
    backends = memory["backends"]
    backend_models = backends.get(backend)
    if not isinstance(backend_models, dict):
        backend_models = {}
        backends[backend] = backend_models
    backend_models[model] = {
        "context_window": window,
        "max_output_tokens": provenance.get("max_output_tokens"),
        "resolved_models": provenance.get("models"),
        "cli_version": provenance.get("version"),
        "observed_at": now.isoformat(),
    }
    document = {"schema_version": 2, "models": memory["models"], "backends": backends}
    try:
        atomic_write(memory_path(paths), json.dumps(document, indent=2, sort_keys=True).encode())
    except OSError:
        return
