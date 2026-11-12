"""Opt-in native CLI launches; no edits to global config or transcript history."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

PROFILE_VERSION = "context-efficiency-v1"
PRESERVE = (
    "Preserve the goal, constraints, current working state, decisions, unresolved "
    "errors, verification evidence and next steps. Keep relevant exact symbols and "
    "commands. Summarize obsolete logs and superseded explorations. Do not discard "
    "unresolved evidence."
)
OUTPUT_POLICY = (
    "Use bounded searches and file ranges. Save verbose logs to files and return a "
    "short summary with relevant errors. Avoid repeating an unchanged failed command. "
    "Preserve diagnostics needed to verify the task."
)


@dataclass
class NativeProfile:
    agent: str
    threshold: str
    version: str
    environment: dict[str, str]
    arguments: list[str]


def build_profile(
    agent: str, threshold: str, window: int, *, codex_keys: set[str] | None = None
) -> NativeProfile:
    if agent not in ("claude", "codex") or threshold not in ("native", "70", "85") or window <= 0:
        raise ValueError("Choose an agent, native/70/85 threshold and positive context window")
    profile = NativeProfile(agent, threshold, PROFILE_VERSION, {}, [])
    if threshold == "native":
        return profile
    if agent == "claude":
        profile.environment = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": threshold}
        profile.arguments = [
            "--append-system-prompt",
            f"Compact Instructions: {PRESERVE}\n{OUTPUT_POLICY}",
        ]
    else:
        if not {"compact_prompt", "model_auto_compact_token_limit"} <= (codex_keys or set()):
            raise ValueError("Codex settings are not verified for this installed version")
        profile.arguments = [
            "-c",
            f"model_auto_compact_token_limit={window * int(threshold) // 100}",
            "-c",
            "compact_prompt=" + json.dumps(PRESERVE),
        ]
    if agent == "codex" and "model_auto_compact_token_limit_scope" in (codex_keys or set()):
        profile.arguments += ["-c", 'model_auto_compact_token_limit_scope="total"']
    return profile


def schema_keys(directory: Path) -> set[str]:
    """Accept actual property definitions, not mere mentions in descriptions."""
    keys: set[str] = set()

    def visit(obj: object) -> None:
        if isinstance(obj, dict):
            properties = obj.get("properties")
            if isinstance(properties, dict):
                keys.update(properties)
            for value in obj.values():
                visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    for path in directory.glob("**/*.json"):
        visit(json.loads(path.read_text(encoding="utf-8")))
    return keys


def detect_codex_keys(executable: str = "codex") -> set[str]:
    resolved = shutil.which(executable)
    if not resolved:
        return set()
    try:
        with tempfile.TemporaryDirectory(prefix="wdi-schema-") as directory:
            result = subprocess.run(
                [resolved, "app-server", "generate-json-schema", "--out", directory],
                capture_output=True,
                timeout=30,
                check=False,
            )
            return schema_keys(Path(directory)) if result.returncode == 0 else set()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", choices=["claude", "codex"])
    parser.add_argument("--threshold", choices=["native", "70", "85"], default="native")
    parser.add_argument("--window", type=int, default=200000)
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch a new interactive CLI; default only prints the profile",
    )
    parser.add_argument("--record", type=Path, help="Save experiment launch metadata locally")
    parser.add_argument("--session-id", help="Associate the run with its resulting transcript")
    parser.add_argument(
        "--outcome",
        choices=["success", "state-loss", "inconclusive"],
        help="Record reviewed continuation quality; requires --record",
    )
    args = parser.parse_args()
    if args.outcome:
        if not args.record or not args.record.is_file():
            parser.error("--outcome requires an existing --record file")
        record = json.loads(args.record.read_text(encoding="utf-8"))
        record.update(
            outcome=args.outcome,
            session_id=args.session_id,
            reviewed_at=datetime.now(UTC).isoformat(),
        )
        args.record.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return
    keys = detect_codex_keys() if args.agent == "codex" and args.threshold != "native" else set()
    try:
        profile = build_profile(args.agent, args.threshold, args.window, codex_keys=keys)
    except ValueError as exc:
        parser.error(str(exc))
    record = asdict(profile)
    record.update(
        created_at=datetime.now(UTC).isoformat(), session_id=args.session_id, outcome="unreviewed"
    )
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if not args.launch:
        print(json.dumps(record, indent=2))
        return
    executable = shutil.which(args.agent)
    if not executable:
        parser.error(f"{args.agent} is not installed")
    # Environment overrides belong only to the child. A normal subsequent launch
    # restores native behavior without changing any files or parent environment.
    result = subprocess.run(
        [executable, *profile.arguments], env={**os.environ, **profile.environment}, check=False
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
