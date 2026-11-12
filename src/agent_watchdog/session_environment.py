"""Bounded environment capture for admitted provider sessions."""

import hashlib
import os
import re
import stat
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from agent_watchdog.analysis import MAX_DIFF_BYTES, _git_output, _Unknown
from agent_watchdog.capture_store import CaptureStore, SessionEnvironment
from agent_watchdog.config import Config, Limits, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.files import open_shared_read
from agent_watchdog.rules.engine import engine_for

SNAPSHOT_SECONDS = 0.5
MAX_FILES = 64
MAX_FILE_BYTES = 1024**2
MAX_IMPORT_DEPTH = 3
_IMPORT = re.compile(r"(?m)^@([^\r\n]+)")
_HOME_ENV = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}
_FILES = {
    "claude": (
        (
            ("CLAUDE.md", "instruction"),
            ("CLAUDE.local.md", "instruction"),
            (".claude/CLAUDE.md", "instruction"),
            ("AGENTS.md", "instruction"),
            (".claude/settings.json", "settings"),
            (".claude/settings.local.json", "settings"),
            (".mcp.json", "settings"),
        ),
        (("CLAUDE.md", "instruction"), ("settings.json", "settings")),
    ),
    "codex": (
        (("AGENTS.md", "instruction"),),
        (
            ("AGENTS.md", "instruction"),
            ("config.toml", "settings"),
            ("hooks.json", "settings"),
        ),
    ),
}


def _add_blob(blobs: dict[str, bytes], raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    blobs.setdefault(digest, raw)
    return digest


def _capture_files(
    provider: str,
    checkout: Path | None,
    provider_home: Path,
    deadline: float,
    blobs: dict[str, bytes],
) -> list[dict[str, str | None]]:
    local, user = _FILES[provider]
    roots = (provider_home, checkout) if checkout is not None else (provider_home,)
    pending = deque((provider_home / name, role, 0) for name, role in user)
    if checkout is not None:
        pending.extendleft((checkout / name, role, 0) for name, role in reversed(local))
    seen: set[Path] = set()
    scheduled: set[Path] = {path for path, _, _ in pending}
    files: list[dict[str, str | None]] = []
    while pending and len(files) < MAX_FILES:
        path, role, depth = pending.popleft()
        entry: dict[str, str | None] = {
            "path": str(path),
            "role": role,
            "digest": None,
            "error": None,
        }
        files.append(entry)
        if time.monotonic() >= deadline:
            entry["error"] = "deadline"
            continue
        try:
            target = path.resolve()
            if not any(target.is_relative_to(root) for root in roots):
                entry["error"] = "outside_roots"
                continue
            if depth and target in seen:
                files.pop()
                continue
            seen.add(target)
            if not stat.S_ISREG(target.stat().st_mode):
                entry["error"] = "unreadable"
                continue
            with open_shared_read(target) as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            if depth:
                entry["error"] = "not_found"
            else:
                files.pop()
            continue
        except OSError, RuntimeError, ValueError:
            entry["error"] = "unreadable"
            continue
        if len(raw) > MAX_FILE_BYTES:
            entry["error"] = "too_large"
            continue
        if time.monotonic() >= deadline:
            entry["error"] = "deadline"
            continue
        entry["digest"] = _add_blob(blobs, raw)
        if role != "instruction" or depth >= MAX_IMPORT_DEPTH:
            continue
        for match in _IMPORT.finditer(raw.decode("utf-8", errors="replace")):
            if len(scheduled) >= MAX_FILES or time.monotonic() >= deadline:
                break
            relative = Path(match[1].strip())
            if relative.is_absolute() or relative.anchor:
                continue
            child = target.parent / relative
            if child not in scheduled:
                scheduled.add(child)
                pending.append((child, "instruction", depth + 1))
    return files


def _git_ref(checkout: Path, args: list[str], deadline: float) -> str | None:
    try:
        return _git_output(checkout, args, deadline).decode("utf-8", "replace").strip() or None
    except _Unknown:
        return None


def _initial_diff(
    checkout: Path | None, head: str | None, deadline: float, blobs: dict[str, bytes]
) -> tuple[str | None, str | None]:
    if checkout is None or head is None:
        return None, "deadline" if time.monotonic() >= deadline else "unknown_head"
    try:
        raw = _git_output(
            checkout,
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--binary",
                head,
                "--",
            ],
            deadline,
        )
    except _Unknown as error:
        return None, error.unknown.reason
    if len(raw) > MAX_DIFF_BYTES:
        return None, "diff_too_large"
    return (_add_blob(blobs, raw) if raw else None), None


def capture_session_environment(
    store: CaptureStore,
    event: Envelope,
    *,
    payload: Mapping[str, object],
    cwd: str | None,
    checkout: Path | None,
    paths: UserPaths,
    config: Config,
    limits: Limits,
) -> None:
    if event.session_id is None or event.provider not in _FILES:
        return
    first = not store.has_session_env(event.provider, event.session_id)
    if not first and event.kind != "session.start":
        return
    captured_at = datetime.now(UTC).isoformat()
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    blobs: dict[str, bytes] = {}
    head = branch = None
    if checkout is not None:
        head = _git_ref(checkout, ["rev-parse", "--verify", "HEAD"], deadline)
        branch = _git_ref(checkout, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline)
    diff_digest, diff_error = (
        _initial_diff(checkout, head, deadline, blobs) if first else (None, None)
    )
    harness = {
        key: payload[key]
        for key in ("model", "permission_mode", "source", "surface")
        if key in payload
    }
    if event.surface != "unknown":
        harness.setdefault("surface", event.surface)
    watchdog: dict[str, object] = {
        "global_policy_intervene": config.defaults.policy_intervene,
        "project_policy_intervene": limits.policy_intervene,
        "limits": limits.model_dump(mode="json"),
    }
    if time.monotonic() < deadline:
        watchdog["rules"] = [
            {
                "name": entry.name,
                "source": entry.source,
                "status": entry.status,
                "digest": entry.digest,
            }
            for entry in engine_for(paths).registry.entries(config)
        ]
    else:
        watchdog["rules_error"] = "deadline"
    configured_home = os.environ.get(_HOME_ENV[event.provider])
    provider_home = (
        Path(configured_home).expanduser().absolute()
        if configured_home
        else Path.home() / f".{event.provider}"
    )
    files = _capture_files(event.provider, checkout, provider_home, deadline, blobs)
    snapshot = SessionEnvironment(
        event.provider,
        event.session_id,
        captured_at,
        cwd,
        str(event.checkout_id) if event.checkout_id is not None else None,
        head,
        branch,
        harness,
        files,
        watchdog,
        diff_digest,
        diff_error,
    )
    store.record_session_env(snapshot, blobs, capture_content=limits.capture_content, limits=limits)
