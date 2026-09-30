"""End-to-end collection readiness (WD-120), distinct from `doctor`'s storage/daemon health.

Each stage reports `ready`, `failed`, or `unknown` with evidence. `unknown` is never
promoted to `ready` by inference: a running daemon, zero loss counters, or an installed
hook do not prove the provider trusts and calls it. The ordinary check is read-only. The
opt-in probe sends one synthetic event through the *installed* adapter command, labels it
as synthetic, and purges it again; it never edits provider settings or invokes a provider.
"""

import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_watchdog import _proc, daemon, resources
from agent_watchdog.config import ConfigError, Project, UserPaths, load_config, project_aliases
from agent_watchdog.hook_install import TARGETS, parse, read, unique_pairs
from agent_watchdog.hooks import EVENTS
from agent_watchdog.storage import StorageError

PROBE_PREFIX = "watchdog-readiness-probe-"
STAGES = ("hook", "project", "provider_callback", "spool", "admission")
_OPTIONS = ("--python", "--config", "--data", "--runtime", "--installation")
_POWERSHELL_ARGUMENT = re.compile(r"'((?:[^']|'')*)'")


def _stage(state: str, action: str | None = None, /, **evidence: object) -> dict:
    stage: dict = {"state": state, "evidence": evidence}
    if action is not None:
        stage["action"] = action
    return stage


def _same_path(left: str, right: object) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(str(right)))


def _hook_failure(reason: str, action: str, **evidence: object) -> tuple[dict, None]:
    return _stage("failed", action, reason=reason, **evidence), None


def _powershell_argv(command: str) -> list[str]:
    """Invert `hook_install.group`'s `commandWindows` quoting; reject any other shape."""
    arguments = [
        match.group(1).replace("''", "'") for match in _POWERSHELL_ARGUMENT.finditer(command)
    ]
    if command != "& " + " ".join(
        "'" + argument.replace("'", "''") + "'" for argument in arguments
    ):
        raise ValueError("Unrecognized Windows hook command")
    return arguments


def _argv(group: dict, provider: str) -> list[str]:
    handlers = group.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
        raise ValueError("Unrecognized hook group")
    handler = handlers[0]
    if provider == "claude":
        arguments = handler.get("args")
        if not isinstance(handler.get("command"), str) or not (
            isinstance(arguments, list) and all(isinstance(item, str) for item in arguments)
        ):
            raise ValueError("Unrecognized Claude hook command")
        return [handler["command"], *arguments]
    key = "commandWindows" if os.name == "nt" else "command"
    if not isinstance(handler.get(key), str):
        raise ValueError("Unrecognized Codex hook command")
    return _powershell_argv(handler[key]) if os.name == "nt" else shlex.split(handler[key])


def _hook(paths: UserPaths, provider: str, target: Path) -> tuple[dict, dict | None]:
    """Validate the installed, owned hook without changing it; return its command."""
    target = target.absolute()
    reinstall = f"Run `agent-watchdog hooks install {provider} --file {target} --apply`."
    if target.name not in TARGETS[provider]:
        return _hook_failure(
            "unsupported_file_name",
            f"Choose the {' or '.join(TARGETS[provider])} file for {provider}.",
        )
    manifest_path = target.with_name(target.name + ".watchdog.json")
    try:
        content = read(target)
        raw = read(manifest_path, limit=8 * 1024**2)
    except (OSError, ValueError) as error:
        return _hook_failure("hook_file_unreadable", reinstall, error_type=type(error).__name__)
    if content is None:
        return _hook_failure("hook_file_missing", reinstall)
    if raw is None:
        return _hook_failure("ownership_record_missing", reinstall)
    try:
        document = parse(content)
        manifest = json.loads(raw, object_pairs_hook=unique_pairs)
        group = manifest["group"]
        valid = (
            isinstance(manifest, dict)
            and manifest.get("schema_version") == 1
            and isinstance(manifest.get("token"), str)
            and isinstance(group, dict)
            and manifest.get("provider", "codex") == provider
            and manifest.get("events") == list(EVENTS[provider])
        )
    except (ValueError, KeyError, TypeError) as error:
        return _hook_failure("hook_file_invalid", reinstall, error_type=type(error).__name__)
    if not valid:
        return _hook_failure("ownership_record_invalid", reinstall)
    for event in manifest["events"]:
        groups = document.get("hooks", {}).get(event, [])
        if any(manifest["token"] in json.dumps(item) and item != group for item in groups):
            return _hook_failure(
                "owned_hook_edited", "Uninstall and reinstall the hook.", event=event
            )
        owned = groups.count(group)
        if owned != 1:
            return _hook_failure(
                "owned_hook_missing" if owned == 0 else "owned_hook_duplicated",
                reinstall,
                event=event,
            )
    try:
        argv = _argv(group, provider)
    except ValueError:
        return _hook_failure("hook_command_unrecognized", reinstall)
    if "-m" in argv[:3]:
        return _hook_failure(
            "python_fallback_command",
            "Install with --adapter-executable or --adapter-artifact; the Python command is "
            "a developer/test utility.",
        )
    options = {
        argv[index]: argv[index + 1] for index in range(1, len(argv) - 1) if argv[index] in _OPTIONS
    }
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        return _hook_failure(
            "executable_missing",
            "Reinstall the native adapter with --adapter-artifact.",
            executable=argv[0],
        )
    python = options.get("--python")
    if python is None or not Path(python).is_file():
        return _hook_failure("python_missing", reinstall, python=python)
    differing = [
        name
        for name, current in (
            ("--config", paths.config),
            ("--data", paths.data),
            ("--runtime", paths.runtime),
        )
        if not _same_path(options.get(name, ""), current)
    ]
    if differing:
        return _hook_failure(
            "hook_targets_another_instance",
            "Pass this hook's own --config/--data/--runtime to the command, or reinstall it.",
            differing=differing,
            hook={name: options.get(name) for name in differing},
        )
    try:
        version = _proc.run(
            [str(executable), "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _hook_failure(
            "adapter_not_runnable",
            "Reinstall the native adapter.",
            executable=str(executable),
            error_type=type(error).__name__,
        )
    banner = version.stdout.strip()
    if version.returncode != 0 or not banner.startswith("agent-watchdog-hook "):
        return _hook_failure(
            "adapter_unrecognized", "Reinstall the native adapter.", executable=str(executable)
        )
    installation = {
        "argv": argv,
        "installed_at": datetime.fromtimestamp(manifest_path.stat().st_mtime, UTC),
    }
    return (
        _stage(
            "ready",
            executable=str(executable),
            adapter_version=banner,
            events=len(EVENTS[provider]),
        ),
        installation,
    )


def _project(paths: UserPaths, reference: str | None) -> tuple[dict, Project | None]:
    from agent_watchdog import inspection

    try:
        project = inspection.project_at(paths, reference)
        alias = project_aliases(load_config(paths.config).projects)[project.id]
    except (StorageError, ConfigError) as error:
        return (
            _stage(
                "failed",
                "Register the project with `project add` or pass --project.",
                reason="project_unresolved",
                error_type=type(error).__name__,
            ),
            None,
        )
    if not project.root.is_dir():
        return (
            _stage(
                "failed",
                "Restore the root or run `project relocate`.",
                reason="project_root_missing",
                project=alias,
            ),
            None,
        )
    return _stage("ready", project=alias), project


def _callback(
    paths: UserPaths, provider: str, project: Project | None, since: datetime | None
) -> dict:
    """Past real events are the only observable trace of native trust; they are not proof of it."""
    from agent_watchdog import inspection

    evidence: dict = {"native_trust": "not_inspected", "events": 0}
    if project is None or since is None:
        return _stage("unknown", reason="no_project_or_hook", **evidence)
    try:
        with inspection.database(paths, project) as db:
            count, last = db.execute(
                "SELECT COUNT(*), MAX(received_at) FROM events "
                "WHERE json_extract(envelope, '$.provider')=? "
                "AND (session_id IS NULL OR session_id NOT LIKE ?) AND received_at>=?",
                (provider, PROBE_PREFIX + "%", since.isoformat()),
            ).fetchone()
            observed = [
                {"provider_version": version, "surface": surface}
                for version, surface in db.execute(
                    "SELECT DISTINCT json_extract(envelope, '$.provider_version'), "
                    "json_extract(envelope, '$.surface') FROM events "
                    "WHERE json_extract(envelope, '$.provider')=? AND received_at>=? "
                    "AND (session_id IS NULL OR session_id NOT LIKE ?) LIMIT 5",
                    (provider, since.isoformat(), PROBE_PREFIX + "%"),
                )
            ]
    except (StorageError, OSError, ValueError, sqlite3.Error) as error:
        return _stage(
            "unknown", reason="database_unavailable", error_type=type(error).__name__, **evidence
        )
    if not count:
        return _stage(
            "unknown",
            "Start a new provider session, review the hook there, then rerun.",
            reason="no_callback_observed_since_install",
            **evidence,
        )
    return _stage(
        "ready",
        events=count,
        last_received_at=last,
        observed=observed,
        native_trust="not_inspected",
        basis="real events received after the hook was installed",
    )


def _delivery(state: str, report: dict, ready: bool) -> tuple[dict, dict] | None:
    """Passive delivery verdict from the daemon's own state; None means "still unknown"."""
    evidence = {"daemon": state}
    if state == "paused":
        stage = _stage(
            "failed",
            "Run `agent-watchdog daemon start`; the adapter drops events while paused.",
            reason="daemon_paused",
            **evidence,
        )
        return stage, stage
    if not ready:
        return None
    if state == "degraded":
        return (
            _stage("unknown", reason="daemon_degraded", **evidence),
            _stage(
                "failed",
                "Inspect `agent-watchdog doctor` and `daemon status`.",
                reason="daemon_degraded",
                errors=bool(report.get("errors")),
                **evidence,
            ),
        )
    return None


def _spool_marker(paths: UserPaths, marker: str) -> Path | None:
    spool = paths.data / "spool"
    try:
        for path in sorted(spool.glob("*.json"))[:512]:
            if path.name != "limits.json" and not path.is_symlink():
                with path.open("rb") as stream:
                    if marker.encode() in stream.read(2 * 1024**2):
                        return path
    except OSError:
        return None
    return None


def _admitted(paths: UserPaths, project: Project, marker: str) -> bool:
    from agent_watchdog import inspection

    try:
        with inspection.database(paths, project) as db:
            return (
                db.execute("SELECT 1 FROM events WHERE session_id=?", (marker,)).fetchone()
                is not None
            )
    except (StorageError, OSError, ValueError, sqlite3.Error):
        return False  # Not created yet, or briefly locked: keep polling.


def _loss_delta(before: dict | None, after: dict | None) -> dict[str, int]:
    if before is None or after is None:
        return {}
    return {key: after[key] - before[key] for key in after if after[key] != before.get(key)}


def _probe(
    paths: UserPaths,
    provider: str,
    project: Project,
    argv: list[str],
    *,
    timeout: float,
    settle: Callable[[], None],
    purge: Callable[[dict], dict],
    state: str,
) -> tuple[dict, dict]:
    marker = PROBE_PREFIX + uuid4().hex
    payload = {"hook_event_name": "SessionStart", "session_id": marker, "cwd": str(project.root)}
    synthetic = {"synthetic": True, "session_id": marker}

    def losses() -> dict | None:
        try:
            return resources.losses(paths.data)
        except (OSError, ValueError):
            return None

    before = losses()
    try:
        completed = _proc.run(
            argv, input=json.dumps(payload), capture_output=True, text=True, timeout=5
        )
        exit_code: int | None = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        exit_code = None
        exit_error = type(error).__name__
    else:
        exit_error = None
    spooled = _spool_marker(paths, marker)
    deadline = time.monotonic() + timeout
    admitted = False
    while True:
        settle()
        admitted = _admitted(paths, project, marker)
        if admitted or time.monotonic() >= deadline:
            break
    if spooled is None and not admitted:
        return (
            _stage(
                "failed",
                "Check `doctor` losses and the adapter's faults.log in the data directory.",
                reason="adapter_did_not_spool",
                adapter_exit_code=exit_code,
                adapter_error_type=exit_error,
                adapter_losses=_loss_delta(before, losses()),
                **synthetic,
            ),
            _stage("unknown", reason="not_spooled", **synthetic),
        )
    spool_stage = _stage(
        "ready",
        delivered="drained_before_check" if spooled is None else "spooled",
        adapter_exit_code=exit_code,
        **synthetic,
    )
    if not admitted:
        removed = "left_in_spool"
        if spooled is not None:
            try:
                spooled.unlink()
                removed = "removed_from_spool"
            except OSError:
                pass
        return (
            spool_stage,
            _stage(
                "failed",
                "Start the daemon (`agent-watchdog daemon start`) and inspect `doctor`.",
                reason="not_admitted_in_time",
                waited_seconds=timeout,
                daemon=state,
                cleanup=removed,
                **synthetic,
            ),
        )
    try:
        purge(
            {
                "action": "purge",
                "project_id": str(project.id),
                "provider": provider,
                "session_id": marker,
            }
        )
        cleanup, action = "purged", None
    except (StorageError, OSError, ValueError):
        cleanup = "purge_failed"
        action = f"Run `agent-watchdog purge {marker} --provider {provider}` to remove the probe."
    return spool_stage, _stage("ready", action, cleanup=cleanup, **synthetic)


def check(
    paths: UserPaths,
    provider: str,
    hook_file: Path,
    *,
    project_ref: str | None,
    probe: bool = False,
    timeout: float = 5.0,
    settle: Callable[[], None] | None = None,
    purge: Callable[[dict], dict] | None = None,
) -> dict:
    """Report readiness stage by stage; `ok` means no stage failed, not that all are ready."""
    hook, installation = _hook(paths, provider, hook_file)
    project_stage, project = _project(paths, project_ref)
    callback = _callback(
        paths, provider, project, installation["installed_at"] if installation else None
    )
    try:
        report = daemon.status(paths)
        state = str(report.get("state", "unavailable"))
    except (OSError, ValueError):
        report, state = {}, "unknown"
    ready = hook["state"] == "ready" and project_stage["state"] == "ready"
    passive = _delivery(state, report, ready)
    blocked = _stage(
        "unknown",
        reason="precondition_not_ready",
        after=[
            name
            for name, stage in (("hook", hook), ("project", project_stage))
            if stage["state"] != "ready"
        ],
    )
    spool, admission = blocked, blocked
    probed = False
    if ready:
        spool = _stage("unknown", reason="no_controlled_event_sent", daemon=state)
        admission = _stage(
            "unknown",
            "Rerun with --probe to send a labeled synthetic event.",
            reason="no_controlled_event_sent",
            daemon=state,
        )
    if passive is not None:
        spool, admission = passive
    elif ready and probe and installation is not None and project is not None:
        probed = True
        spool, admission = _probe(
            paths,
            provider,
            project,
            installation["argv"],
            timeout=timeout,
            settle=settle or (lambda: time.sleep(0.1)),
            purge=purge or (lambda request: daemon.request_control(paths, request)),
            state=state,
        )
        callback = _callback(paths, provider, project, installation["installed_at"])
    stages = {
        "hook": hook,
        "project": project_stage,
        "provider_callback": callback,
        "spool": spool,
        "admission": admission,
    }
    return {
        "provider": provider,
        "probe": "synthetic-local" if probed else "none",
        "ok": all(stage["state"] != "failed" for stage in stages.values()),
        "stages": stages,
    }
