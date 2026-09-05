"""Bounded process-lifetime experiment; never starts a model conversation."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def wait_for(path: Path, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {path.name}")
        time.sleep(0.05)


def child(directory: Path) -> None:
    try:
        (directory / "ready").touch()
        wait_for(directory / "release")
        (directory / "response").touch()
    finally:
        (directory / "finished").touch()


def parent(directory: Path, capture_hook: bool) -> None:
    if capture_hook:
        payload = json.load(sys.stdin)
        # Keep schema evidence only, never session paths, identifiers, or content.
        evidence = {
            "event": payload.get("hook_event_name"),
            "field_types": {key: type(value).__name__ for key, value in payload.items()},
        }
        (directory / "hook.json").write_text(json.dumps(evidence), encoding="utf-8")
    options = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--mode", "child", "--dir", str(directory)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )
    wait_for(directory / "ready")


def probe(claude_init: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="watchdog-probe-") as temporary:
        directory = Path(temporary)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--mode",
            "parent",
            "--dir",
            str(directory),
        ]
        if claude_init:
            executable = shutil.which("claude")
            if executable is None:
                raise RuntimeError("Claude CLI is not installed")
            # Claude's command hooks use a shell. Restrict this probe to plain paths.
            paths = [
                Path(sys.executable).as_posix(),
                Path(__file__).resolve().as_posix(),
                directory.as_posix(),
            ]
            if any(any(char in path for char in '"$`\n\r') for path in paths):
                raise ValueError("Probe paths contain unsupported shell characters")
            hook_command = (
                f'"{paths[0]}" "{paths[1]}" --mode parent --dir "{paths[2]}" --capture-hook'
            )
            settings = directory / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": hook_command,
                                            "timeout": 20,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            command = [
                executable,
                "--init-only",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--settings",
                str(settings),
            ]
        try:
            result = subprocess.run(
                command,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError(f"Parent exited with code {result.returncode}")
            if not (directory / "ready").exists():
                raise RuntimeError("Parent exited without starting the probe child")
            (directory / "release").touch()
            wait_for(directory / "response")
            wait_for(directory / "finished")
            evidence = {
                "parent": "claude-init-only" if claude_init else "python",
                "platform": sys.platform,
                "parent_exited_before_release": True,
                "parent_stdout_empty": result.stdout == "",
                "child_responded": True,
                "child_finished": True,
            }
            if claude_init:
                evidence["hook"] = json.loads((directory / "hook.json").read_text())
            return evidence
        finally:
            (directory / "release").touch()
            if (directory / "ready").exists():
                wait_for(directory / "finished")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["probe", "parent", "child"], default="probe")
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--capture-hook", action="store_true")
    parser.add_argument("--claude-init", action="store_true")
    args = parser.parse_args()
    if args.mode != "probe" and args.dir is None:
        parser.error("--dir is required for parent/child mode")
    if args.mode == "parent":
        parent(args.dir, args.capture_hook)
    elif args.mode == "child":
        child(args.dir)
    else:
        print(json.dumps(probe(args.claude_init)))


if __name__ == "__main__":
    main()
