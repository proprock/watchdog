"""Demotion of provisional Codex control profiles (WD-180).

A provisional profile acts on an app/backend pair that no provider probe has covered.
Its trust is conditional on one observation: a call the daemon denied must not finish.
When a remembered denial is followed by a ``tool.finish`` of the same call, the profile
is switched to log-only. The record lives beside the other daemon state and survives a
restart; deleting it re-enables the profile after a new probe has confirmed it.
"""

import json
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from agent_watchdog.config import UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.files import atomic_write

_FILE = "profile-demotions.json"
_REMEMBERED = 256

_lock = threading.Lock()
# (session, tool_use_id) -> profile of denials the daemon delivered; bounded, in memory,
# because the contradicting event arrives within the same turn.
_denials: OrderedDict[tuple[str, str], str] = OrderedDict()
# path -> (mtime_ns, demoted profiles); None when the record is unreadable.
_cache: dict[Path, tuple[int, frozenset[str] | None]] = {}


def reset() -> None:
    with _lock:
        _denials.clear()
        _cache.clear()


def remember_denial(session_id: str, tool_use_id: str, profile: str) -> None:
    with _lock:
        _denials[(session_id, tool_use_id)] = profile
        while len(_denials) > _REMEMBERED:
            _denials.popitem(last=False)


def _tool_use_id(event: Envelope) -> str | None:
    namespace = event.payload.get(event.provider)
    value = namespace.get("tool_use_id") if isinstance(namespace, dict) else None
    return value if isinstance(value, str) and value else None


def observe(paths: UserPaths, event: Envelope) -> str | None:
    """Demote and return the profile when a remembered denial's call finished."""
    if event.provider != "codex" or event.kind != "tool.finish" or event.session_id is None:
        return None
    tool_use_id = _tool_use_id(event)
    if tool_use_id is None:
        return None
    with _lock:
        profile = _denials.pop((event.session_id, tool_use_id), None)
    if profile is None:
        return None
    demote(paths, profile, evidence=tool_use_id)
    return profile


def _record(paths: UserPaths) -> dict[str, dict[str, str]]:
    file = paths.data / _FILE
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    profiles = value.get("profiles") if isinstance(value, dict) else None
    if not isinstance(profiles, dict):
        raise ValueError("unreadable demotion record")
    return profiles


def demote(paths: UserPaths, profile: str, *, evidence: str) -> None:
    with _lock:
        try:
            profiles = _record(paths)
        except (OSError, ValueError):
            profiles = {}
        profiles[profile] = {"demoted_at": datetime.now(UTC).isoformat(), "evidence": evidence}
        atomic_write(
            paths.data / _FILE,
            (json.dumps({"profiles": profiles}, indent=2) + "\n").encode(),
        )
        _cache.pop(paths.data / _FILE, None)


def is_demoted(paths: UserPaths, profile: str) -> bool:
    """Unknown is not zero: a record that cannot be read demotes every profile."""
    file = paths.data / _FILE
    try:
        mtime = file.stat().st_mtime_ns
    except FileNotFoundError:
        return False
    except OSError:
        return True
    with _lock:
        cached = _cache.get(file)
        if cached is None or cached[0] != mtime:
            try:
                names: frozenset[str] | None = frozenset(_record(paths))
            except (OSError, ValueError):
                names = None
            cached = _cache[file] = (mtime, names)
    return cached[1] is None or profile in cached[1]
