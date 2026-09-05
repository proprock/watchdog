import argparse
import json
import sys
import time
from pathlib import Path

from agent_watchdog import daemon
from agent_watchdog.config import UserPaths, user_paths
from agent_watchdog.hook_install import change
from agent_watchdog.hooks import observe


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-watchdog",
        description="Local watchdog core and metadata-only Codex observation hooks.",
    )
    parser.add_argument("--home", type=Path, help="Isolated config, data and runtime directory")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--runtime", type=Path)
    commands = parser.add_subparsers(dest="command")
    core = commands.add_parser("daemon", help="Control the local background process")
    core.add_argument("action", choices=("start", "stop", "pause", "status", "run"))
    hook = commands.add_parser("hook", help="Observe one native hook from stdin")
    hook.add_argument("provider", choices=("codex",))
    hook.add_argument("--installation", help=argparse.SUPPRESS)
    hooks = commands.add_parser("hooks", help="Preview or apply a Codex hooks.json edit")
    hooks.add_argument("action", choices=("install", "uninstall"))
    hooks.add_argument("provider", choices=("codex",))
    hooks.add_argument("--file", type=Path, required=True)
    hooks.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    paths = user_paths()
    if args.home:
        home = args.home.resolve()
        paths = UserPaths(home / "config.toml", home / "data", home / "runtime")
    paths = UserPaths(
        (args.config or paths.config).resolve(),
        (args.data or paths.data).resolve(),
        (args.runtime or paths.runtime).resolve(),
    )
    try:
        if args.command == "hook":
            try:
                observe(paths, sys.stdin.buffer)
            except KeyboardInterrupt:
                pass
            finally:
                print("{}")
            return 0
        if args.command == "hooks":
            print(
                json.dumps(
                    change(args.file, paths, install=args.action == "install", apply=args.apply)
                )
            )
            return 0
        if args.action == "run":
            return daemon.run(paths)
        if args.action == "start":
            daemon.start(paths)
        elif args.action in ("stop", "pause"):
            daemon.stop(paths)
        deadline = time.monotonic() + 10
        while True:
            report = daemon.status(paths)
            if (
                args.action == "status"
                or (
                    args.action == "start"
                    and report["alive"]
                    and report.get("acknowledged_request") == report["request_id"]
                )
                or (args.action in ("stop", "pause") and not report["alive"])
            ):
                print(json.dumps(report))
                return 0
            if time.monotonic() >= deadline:
                print(json.dumps(report | {"error": "Daemon control timed out"}))
                return 1
            time.sleep(0.05)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__}))
        return 1
    except KeyboardInterrupt:
        return 130
