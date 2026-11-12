"""Scratch-only Watchdog installation for one opt-in Codex control probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import tomli_w

from agent_watchdog.config import Rules, UserPaths, load_config, save_config
from agent_watchdog.daemon import _drain_spool, mutate_registry
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store

PROFILE = "windows-cli-exec-0.162.0-alpha.17.2"
PROVIDER_VERSION = "0.162.0-alpha.17.2"
RULE_NAME = "codex_probe_deny_patch"
CASE = "pre-tool-apply-patch-deny"
DENY_REASON = "WD139C_DENY"


def paths_at(scratch: Path) -> UserPaths:
    root = scratch / "watchdog"
    return UserPaths(root / "config.toml", root / "data", root / "runtime")


def require_provider_version(version: str) -> None:
    if version != f"codex-cli {PROVIDER_VERSION}":
        raise ValueError(f"production adapter probe requires Codex CLI {PROVIDER_VERSION}")


def adapter_version(adapter: Path) -> str:
    if not adapter.is_absolute() or not adapter.is_file():
        raise ValueError("--production-adapter must name an existing absolute file")
    result = subprocess.run(
        [str(adapter), "--version"], capture_output=True, text=True, check=True, timeout=5
    )
    banner = result.stdout.strip()
    if "(providers: codex, claude; decision schema 2)" not in banner:
        raise ValueError("production adapter lacks the required decision schema")
    return banner


def _hook_command(adapter: Path, paths: UserPaths, log: Path) -> str:
    arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--hook",
        "--adapter",
        str(adapter),
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        "--log",
        str(log),
    ]
    portable = shlex.join(arguments)
    windows = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
    fields = [
        "[[hooks.SessionStart]]",
        'matcher = "^startup$"',
        "",
        "[[hooks.SessionStart.hooks]]",
        'type = "command"',
        f"command = {json.dumps(portable)}",
        f"commandWindows = {json.dumps(windows)}",
        "timeout = 30",
        "additionalContextLimit = 128",
        "",
        "[[hooks.PreToolUse]]",
        'matcher = "^apply_patch$"',
        "",
        "[[hooks.PreToolUse.hooks]]",
        'type = "command"',
        f"command = {json.dumps(portable)}",
        f"commandWindows = {json.dumps(windows)}",
        "timeout = 30",
        "additionalContextLimit = 128",
        "",
    ]
    return "\n".join(fields)


def configure(scratch: Path, work: Path, log: Path, adapter: Path) -> tuple[UserPaths, str]:
    """Approve one declarative rule in the scratch installation only."""
    if not all(path.is_absolute() for path in (scratch, work, log, adapter)):
        raise ValueError("production paths must be absolute")
    paths = paths_at(scratch)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    mutate_registry(paths, lambda registry: registry.add(work))
    rule_path = paths.config.parent / "rules" / f"{RULE_NAME}.toml"
    rule_path.parent.mkdir(exist_ok=True)
    # The message matches the independent provider-effect verdict used by the
    # diagnostic case; only the approved hash enables this rule.
    rule = {
        "schema_version": 1,
        "name": RULE_NAME,
        "version": "1",
        "origin": "user",
        "provider": ["codex"],
        "event": "PreToolUse",
        "match": {"tool_name": "apply_patch"},
        "action": {"kind": "deny", "message": DENY_REASON},
    }
    rule_path.write_text(tomli_w.dumps(rule), encoding="utf-8", newline="\n")
    config = load_config(paths.config)
    approved = {RULE_NAME: hashlib.sha256(rule_path.read_bytes()).hexdigest()}
    save_config(paths.config, config.model_copy(update={"rules": Rules(approved=approved)}))
    return paths, _hook_command(adapter, paths, log)


def relay_adapter(payload: dict[str, object], adapter: Path, paths: UserPaths, log: Path) -> bytes:
    """Forward the real adapter response exactly; keep callback diagnostics local."""
    command = [
        str(adapter),
        "--python",
        sys.executable,
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        "--codex-control-profile",
        PROFILE,
        "hook",
        "codex",
    ]
    result = subprocess.run(
        command,
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=20,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError, UnicodeDecodeError:
        response = {}
    with log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                {
                    "event": payload.get("hook_event_name"),
                    "input": payload,
                    "output": response,
                    "adapter_exit_code": result.returncode,
                }
            )
            + "\n"
        )
    return result.stdout


def readback(paths: UserPaths) -> dict[str, int]:
    """Ingest native spool with daemon code, then read persisted control facts."""
    config = load_config(paths.config)
    spool_files = list((paths.data / "spool").glob("*.json"))
    patch_event_ids: set[str] = set()
    for path in spool_files:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        hook = item.get("input")
        if (
            item.get("provider") == "codex"
            and isinstance(hook, dict)
            and hook.get("hook_event_name") == "PreToolUse"
            and hook.get("tool_name") == "apply_patch"
            and isinstance(item.get("event_id"), str)
        ):
            patch_event_ids.add(item["event_id"])
    _drain_spool(paths, config)
    project = config.projects[0]
    project_data = paths.project_data(project.id)
    with Store(project_data, project.id) as store:
        events = store.events(limit=1000)
    controls = [event for event in events if event.kind == "control" and event.provider == "codex"]
    return {
        "spooled": len(spool_files),
        "spooled_patch_events": len(patch_event_ids),
        "stored_controls": len(controls),
        "persisted_controls": confirmed_controls(controls, patch_event_ids),
    }


def confirmed_controls(events: list[Envelope], patch_event_ids: set[str]) -> int:
    """Count only delivered approved denies linked to this native callback."""
    return sum(
        event.kind == "control"
        and event.provider == "codex"
        and event.source == "daemon"
        and isinstance((payload := event.payload.get("codex")), dict)
        and payload.get("action") == "deny"
        and payload.get("rule") == RULE_NAME
        and isinstance(payload.get("delivered_at"), str)
        and bool(payload["delivered_at"])
        and isinstance(payload.get("evidence_ids"), list)
        and any(event_id in patch_event_ids for event_id in payload["evidence_ids"])
        for event in events
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true", required=True)
    for name in ("adapter", "config", "data", "runtime", "log"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    paths = UserPaths(args.config, args.data, args.runtime)
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized callback")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid callback")
        sys.stdout.buffer.write(relay_adapter(payload, args.adapter, paths, args.log))
    except OSError, ValueError, subprocess.SubprocessError:
        # A failed observer must never stop the provider.
        sys.stdout.buffer.write(b"{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
