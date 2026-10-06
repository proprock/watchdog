"""Opt-in, isolated live probes for provider hook contracts.

This module contains only the Codex context probe for WD-139.  It is never
called by the offline test suite or the normal Watchdog command-line interface.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.config import load_config, user_paths

USER_PROMPT_MARKER = "WD139_USER_PROMPT_CONTEXT"
POST_TOOL_MARKER = "WD139_POST_TOOL_CONTEXT"
EVENT_MARKERS = {
    "UserPromptSubmit": USER_PROMPT_MARKER,
    "PostToolUse": POST_TOOL_MARKER,
}
MAX_HOOK_INPUT_BYTES = 1024 * 1024
MAX_ROLLOUT_BYTES = 16 * 1024 * 1024


def hook_response(event: str) -> dict[str, object]:
    """Return the documented Codex additional-context response for one event."""
    try:
        marker = EVENT_MARKERS[event]
    except KeyError as error:
        raise ValueError(f"unsupported live-probe event: {event}") from error
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": marker,
        }
    }


def inside_registered_root(path: Path, roots: Sequence[Path]) -> bool:
    """Whether *path* resolves below one of the configured Watchdog projects."""
    try:
        candidate = path.resolve()
    except OSError:
        return True
    for root in roots:
        try:
            if candidate.is_relative_to(root.resolve()):
                return True
        except OSError:
            return True
    return False


def registered_roots(config_path: Path | None) -> list[Path]:
    """Read configured project roots without changing the user configuration."""
    config = load_config(config_path or user_paths().config)
    return [project.root for project in config.projects]


def ensure_scratch_outside_roots(scratch: Path, roots: Sequence[Path]) -> None:
    if inside_registered_root(scratch, roots):
        raise ValueError("refusing a scratch repository inside a registered Watchdog project")


def _append_log(log: Path, record: dict[str, object]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def handle(log: Path) -> int:
    """Record one raw scratch callback and print its documented JSON response."""
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("hook input exceeds the probe limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        event = payload.get("hook_event_name")
        if not isinstance(event, str):
            raise ValueError("hook input has no event name")
        response = hook_response(event)
        _append_log(log, {"event": event, "input": payload, "output": response})
        print(json.dumps(response, sort_keys=True))
    except (OSError, TypeError, ValueError) as error:
        _append_log(log, {"error": type(error).__name__})
        print("{}")
    return 0


def command_windows(python: Path, handler: Path, log: Path) -> str:
    arguments = [str(python), str(handler), "handler", "--log", str(log)]
    return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)


def profile_configuration(handler: Path, log: Path) -> str:
    """Render a temporary user-level profile so scratch trust cannot skip hooks."""
    command = shlex.join([sys.executable, str(handler), "handler", "--log", str(log)])
    command_windows_value = command_windows(Path(sys.executable), handler, log)
    fields = [
        'type = "command"',
        f"command = {json.dumps(command)}",
        "timeout = 30",
        "additionalContextLimit = 128",
    ]
    if os.name == "nt":
        fields.insert(2, f"commandWindows = {json.dumps(command_windows_value)}")
    return "\n".join(
        [
            "[[hooks.UserPromptSubmit]]",
            "",
            "[[hooks.UserPromptSubmit.hooks]]",
            *fields,
            "",
            "[[hooks.PostToolUse]]",
            'matcher = "^Bash$"',
            "",
            "[[hooks.PostToolUse.hooks]]",
            *fields,
            "",
        ]
    )


def codex_command(codex: str, profile_name: str, scratch: Path, prompt: str) -> list[str]:
    return [
        codex,
        "exec",
        "--enable",
        "hooks",
        "--profile",
        profile_name,
        "--dangerously-bypass-hook-trust",
        "--sandbox",
        "read-only",
        "-C",
        str(scratch),
        prompt,
    ]


def _read_records(log: Path) -> list[dict[str, object]]:
    if not log.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _rollout_path(records: Sequence[dict[str, object]]) -> Path | None:
    for record in records:
        payload = record.get("input")
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("transcript_path")
        if isinstance(candidate, str) and candidate:
            return Path(candidate)
    return None


def _read_rollout(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_ROLLOUT_BYTES + 1).decode("utf-8", errors="replace")
    except OSError:
        return ""


def verify(
    records: Sequence[dict[str, object]], stdout: str, rollout: str
) -> tuple[str, list[str]]:
    """Classify only the evidence that this one probe actually collected."""
    observed: dict[str, int] = {event: 0 for event in EVENT_MARKERS}
    returned: set[str] = set()
    for record in records:
        event = record.get("event")
        if not isinstance(event, str) or event not in EVENT_MARKERS:
            continue
        observed[event] += 1
        output = record.get("output")
        if not isinstance(output, dict):
            continue
        specific = output.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            continue
        if (
            specific.get("hookEventName") == event
            and specific.get("additionalContext") == EVENT_MARKERS[event]
        ):
            returned.add(event)

    reasons = [f"missing callback: {event}" for event, count in observed.items() if count != 1]
    reasons.extend(
        f"missing expected response: {event}" for event in EVENT_MARKERS if event not in returned
    )
    for marker in EVENT_MARKERS.values():
        if marker not in stdout:
            reasons.append(f"marker absent from Codex response: {marker}")
        if marker not in rollout:
            reasons.append(f"marker absent from rollout: {marker}")
    return ("supported" if not reasons else "inconclusive", reasons)


def _result(
    *,
    version: str | None,
    records: Sequence[dict[str, object]],
    state: str,
    limitations: Sequence[str],
    exit_code: int | None,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    callbacks: dict[str, dict[str, object]] = {}
    for event in EVENT_MARKERS:
        event_records = [record for record in records if record.get("event") == event]
        input_fields = {
            key
            for record in event_records
            for payload in [record.get("input")]
            if isinstance(payload, dict)
            for key in payload
            if isinstance(key, str)
        }
        callbacks[event] = {
            "observed": len(event_records),
            "input_fields": sorted(input_fields),
            "response": hook_response(event),
        }
    return {
        "format_version": "watchdog.wd139-codex-context.v1",
        "provenance": {
            "provider": "codex",
            "version": version,
            "surface": "cli",
            "os": {"name": platform.system(), "architecture": platform.machine()},
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "callbacks": callbacks,
        "result": {"state": state, "limitations": list(limitations)},
        "cleanup": {
            "scratch_repository": "removed",
            "temporary_profile": "removed",
            "user_configuration": "unchanged",
        },
        "codex_exit_code": exit_code,
    }


def resolve_codex(command: str) -> str:
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError("could not find Codex CLI; add codex to PATH or pass --codex PATH")
    return resolved


def codex_version(codex: str) -> str:
    result = subprocess.run(
        [codex, "--version"],
        capture_output=True,
        check=True,
        text=True,
        creationflags=hidden_creationflags(),
    )
    return result.stdout.strip()


def require_standalone_cli() -> None:
    if os.environ.get("CODEX_APP_TOOLS_PIPE_PATH") or os.environ.get("CODEX_THREAD_ID"):
        raise RuntimeError("run the live probe from a standalone PowerShell or cmd.exe console")


def run_codex_context(args: argparse.Namespace) -> dict[str, object]:
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    require_standalone_cli()
    roots = registered_roots(args.config)
    temporary_parent = Path(tempfile.gettempdir())
    ensure_scratch_outside_roots(temporary_parent, roots)
    codex = resolve_codex(args.codex)
    version = codex_version(codex)
    with tempfile.TemporaryDirectory(prefix="watchdog-wd139-codex-") as temporary:
        scratch = Path(temporary)
        ensure_scratch_outside_roots(scratch, roots)
        log = scratch / "probe.ndjson"
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        profile_name = f"watchdog-wd139-{uuid4().hex}"
        profile = home / f"{profile_name}.config.toml"
        if profile.exists():
            raise RuntimeError("unexpected existing temporary Codex probe profile")
        profile.write_text(
            profile_configuration(Path(__file__).resolve(), log), encoding="utf-8", newline="\n"
        )
        prompt = (
            "Run exactly one Bash command: echo WD139_SAFE_TOOL. After it completes, reply with "
            "the two hook-injected tokens, one per line, and nothing else. Do not guess tokens "
            "that you have not received."
        )
        try:
            subprocess.run(
                ["git", "init", str(scratch)],
                capture_output=True,
                text=True,
                check=True,
                creationflags=hidden_creationflags(),
            )
            command = codex_command(codex, profile_name, scratch, prompt)
            completed: subprocess.CompletedProcess[str] | None = None
            failure: str | None = None
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    creationflags=hidden_creationflags(),
                )
            except subprocess.TimeoutExpired:
                failure = "Codex timed out before the probe completed"
            records = _read_records(log)
            rollout = _read_rollout(_rollout_path(records))
            stdout = completed.stdout if completed is not None else ""
            state, limitations = verify(records, stdout, rollout)
            if failure is not None:
                limitations.append(failure)
            if completed is not None and completed.returncode != 0:
                limitations.append("Codex exited nonzero")
            if limitations:
                state = "inconclusive"
            return _result(
                version=version,
                records=records,
                state=state,
                limitations=limitations,
                exit_code=completed.returncode if completed is not None else None,
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        finally:
            profile.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    handler = commands.add_parser("handler", help="internal Codex hook handler")
    handler.add_argument("--log", type=Path, required=True)
    probe = commands.add_parser("codex-context", help="run the isolated Codex context probe")
    probe.add_argument("--codex", default="codex")
    probe.add_argument("--config", type=Path)
    probe.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.action == "handler":
        return handle(args.log)
    try:
        print(json.dumps(run_codex_context(args), sort_keys=True))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"result": {"state": "inconclusive", "limitations": [str(error)]}}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
