"""Explicit JSON hook edits with exact ownership, backups, and native trust intact."""

import copy
import hashlib
import json
import shlex
import sys
from pathlib import Path
from uuid import uuid4

from agent_watchdog.config import UserPaths
from agent_watchdog.files import atomic_write
from agent_watchdog.hooks import EVENTS
from agent_watchdog.storage import writer_lock


def unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key; file left unchanged")
        result[key] = value
    return result


def read(path: Path, limit: int = 1024**2) -> bytes | None:
    if path.is_symlink():
        raise ValueError("Refusing a symlink hook file")
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Hook file exceeds its size limit")
        return content
    except FileNotFoundError:
        return None


TARGETS: dict[str, tuple[str, ...]] = {
    "codex": ("hooks.json",),
    "claude": ("settings.json", "settings.local.json"),
}


def parse(content: bytes | None) -> dict:
    value = json.loads(content, object_pairs_hook=unique_pairs) if content is not None else {}
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError("Expected a hook configuration object")
    for groups in value.get("hooks", {}).values():
        if not isinstance(groups, list) or any(
            not isinstance(group, dict)
            or not isinstance(group.get("hooks"), list)
            or any(not isinstance(handler, dict) for handler in group["hooks"])
            for group in groups
        ):
            raise ValueError("Invalid hook matcher groups")
    return value


def group(
    paths: UserPaths,
    token: str,
    adapter_executable: Path | None = None,
    provider: str = "codex",
) -> dict:
    arguments = [
        sys.executable,
        "-m",
        "agent_watchdog",
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        "hook",
        provider,
        "--installation",
        token,
    ]
    if adapter_executable is not None:
        arguments[:3] = [str(adapter_executable), "--python", sys.executable]
    if provider == "claude":
        # Exec form: Claude spawns `command` directly with `args`, no shell, so
        # paths with quotes, $ or backticks never reach a parser. No commandWindows
        # (Claude has no such field) and no matcher (optional; absent matches all).
        return {
            "hooks": [
                {
                    "type": "command",
                    "command": arguments[0],
                    "args": arguments[1:],
                    "timeout": 2,
                }
            ]
        }
    return {
        "hooks": [
            {
                "type": "command",
                "command": shlex.join(arguments),
                "commandWindows": "& "
                + " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments),
                "timeout": 2,
            }
        ]
    }


def encode(document: dict) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def change(
    target: Path,
    paths: UserPaths,
    *,
    install: bool,
    apply: bool,
    adapter_executable: Path | None = None,
    provider: str = "codex",
) -> dict:
    if provider not in TARGETS:
        raise ValueError("Unknown hook provider")
    if adapter_executable is not None:
        if not install or not adapter_executable.is_absolute() or not adapter_executable.is_file():
            raise ValueError("Choose an existing absolute adapter executable for installation")
    target = target.absolute()
    if target.name not in TARGETS[provider]:
        raise ValueError(f"Choose an explicit {' or '.join(TARGETS[provider])} file for {provider}")
    if not apply:
        return _change(
            target,
            paths,
            install=install,
            apply=False,
            adapter_executable=adapter_executable,
            provider=provider,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(target.with_name(target.name + ".watchdog.lock")):
        return _change(
            target,
            paths,
            install=install,
            apply=True,
            adapter_executable=adapter_executable,
            provider=provider,
        )


def _change(
    target: Path,
    paths: UserPaths,
    *,
    install: bool,
    apply: bool,
    adapter_executable: Path | None = None,
    provider: str = "codex",
) -> dict:
    original = read(target)
    document = parse(original)
    manifest_path = target.with_name(target.name + ".watchdog.json")
    manifest_raw = read(manifest_path, limit=8 * 1024**2)
    manifest: dict | None = (
        json.loads(manifest_raw, object_pairs_hook=unique_pairs)
        if manifest_raw is not None
        else None
    )
    # Codex manifests written before multi-provider support carry no "provider" key.
    record_provider = manifest.get("provider", "codex") if isinstance(manifest, dict) else "codex"
    if manifest_raw is not None and (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("token"), str)
        or not isinstance(manifest.get("group"), dict)
        or record_provider != provider
        or manifest.get("events") != list(EVENTS[provider])
        or "original" not in manifest
        or (manifest["original"] is not None and not isinstance(manifest["original"], str))
    ):
        raise ValueError("Invalid installation record; files left unchanged")
    if manifest is None:
        if "--installation" in json.dumps(document):
            raise ValueError(
                "Existing Watchdog hooks have no ownership record; restore that record first"
            )
        if not install:
            return {"changed": False, "file": str(target)}
        token = str(uuid4())
        manifest = {
            "schema_version": 1,
            "token": token,
            "provider": provider,
            "group": group(paths, token, adapter_executable, provider),
            "events": list(EVENTS[provider]),
            "original": original.decode("utf-8") if original is not None else None,
        }
    expected = manifest["group"]
    previous = manifest["original"]
    baseline = parse(previous.encode() if previous is not None else None)
    updated = copy.deepcopy(document)
    hooks = updated.setdefault("hooks", {})
    for event in manifest["events"]:
        groups = hooks.get(event, [])
        for candidate in groups:
            if manifest["token"] in json.dumps(candidate) and candidate != expected:
                raise ValueError(
                    "An owned hook was edited; resolve it manually before changing installation"
                )
        owned = groups.count(expected)
        if owned > 1:
            raise ValueError("Duplicate owned hook; file left unchanged")
        if install and not owned:
            hooks.setdefault(event, []).append(expected)
        elif not install and owned:
            groups.remove(expected)
            if not groups:
                if baseline.get("hooks", {}).get(event) == []:
                    hooks[event] = []
                else:
                    hooks.pop(event)
    if install and expected != group(paths, manifest["token"], adapter_executable, provider):
        raise ValueError("Installation paths changed; uninstall the recorded installation first")
    if not install and not hooks and "hooks" not in baseline:
        updated.pop("hooks")
    if not install and updated == baseline:
        result = previous.encode() if previous is not None else None
    else:
        result = encode(updated)
    if result is not None and len(result) > 1024**2:
        raise ValueError("Updated hook configuration would exceed 1 MiB")
    changed = updated != document or (not install and result != original)
    notice = (
        "Review through Claude `/hooks`, then restart the session. "
        "This command never changes trust."
        if provider == "claude"
        else "Review through Codex /hooks, then restart. This command never changes trust."
    )
    report = {
        "changed": changed,
        "file": str(target),
        "configuration": updated,
        "notice": notice,
    }
    if not apply:
        return report
    if changed:
        if original is not None:
            backup = target.with_name(
                target.name + ".watchdog-backup-" + hashlib.sha256(original).hexdigest() + ".json"
            )
            existing = read(backup)
            if existing is not None and existing != original:
                raise ValueError("Backup content conflict")
            atomic_write(backup, original)
            report["backup"] = str(backup)
        # Save ownership first: interrupted installs can be completed or uninstalled safely.
        if install:
            atomic_write(manifest_path, encode(manifest))
        if read(target) != original:
            raise ValueError("Hook configuration changed concurrently; retry after reviewing it")
        if result is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, result)
    if not install:
        manifest_path.unlink(missing_ok=True)
    return report
