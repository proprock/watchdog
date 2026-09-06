import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from uuid import UUID

from agent_watchdog import daemon
from agent_watchdog.config import UserPaths, load_config, user_paths
from agent_watchdog.hook_install import change
from agent_watchdog.hooks import observe
from agent_watchdog.registry import RegistryError
from agent_watchdog.storage import StorageError


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-watchdog",
        description="Local watchdog core, Codex observation hooks, and session inspection.",
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
    hooks.add_argument("--adapter-executable", type=Path, help="Absolute native adapter path")
    hooks.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    projects = commands.add_parser("project", help="Manage explicitly registered projects")
    actions = projects.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    actions.add_parser("add").add_argument("path", type=Path)
    actions.add_parser("remove").add_argument("project_id", type=UUID)
    relocate = actions.add_parser("relocate")
    relocate.add_argument("project_id", type=UUID)
    relocate.add_argument("path", type=Path)
    commands.add_parser("doctor", help="Inspect configuration, storage, and daemon health")
    sessions = commands.add_parser(
        "sessions", help="Read collected sessions without starting collection"
    )
    views = sessions.add_subparsers(dest="action", required=True)
    for action in ("list", "show"):
        view = views.add_parser(action)
        view.add_argument("--project", type=UUID, help="Project UUID; default resolves cwd")
        view.add_argument("--limit", type=int, default=100)
        view.add_argument("--offset", type=int, default=0)
        if action == "show":
            view.add_argument("session_id", nargs="?")
            view.add_argument(
                "--unassigned", action="store_true", help="Show events without session identity"
            )
            view.add_argument("--provider", default="codex")
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
        if args.command == "project":
            if args.action == "list":
                result = {
                    "projects": [
                        project.model_dump(mode="json")
                        for project in load_config(paths.config).projects
                    ]
                }
            else:
                from agent_watchdog.registry import Registry

                def mutate(registry: Registry) -> None:
                    nonlocal result
                    if args.action == "add":
                        result = registry.add(args.path).model_dump(mode="json")
                    elif args.action == "relocate":
                        result = registry.relocate(args.project_id, args.path).model_dump(
                            mode="json"
                        )
                    else:
                        registry.remove(args.project_id)
                        result = {"removed": str(args.project_id), "data_deleted": False}

                result = {}
                daemon.mutate_registry(paths, mutate)
            print(json.dumps(result))
            return 0
        if args.command in ("doctor", "sessions"):
            from agent_watchdog import inspection

            if args.command == "doctor":
                report = inspection.doctor(paths)
                print(json.dumps(report))
                return 0 if report["ok"] else 1
            project = inspection.project_at(paths, args.project)
            if args.action == "list":
                result = inspection.sessions(paths, project, limit=args.limit, offset=args.offset)
            else:
                if (args.session_id is None) == (not args.unassigned):
                    raise StorageError("Select a session ID or --unassigned")
                result = inspection.show(
                    paths,
                    project,
                    args.session_id,
                    provider=args.provider,
                    limit=args.limit,
                    offset=args.offset,
                )
            print(json.dumps(result))
            return 0
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
                    change(
                        args.file,
                        paths,
                        install=args.action == "install",
                        apply=args.apply,
                        adapter_executable=args.adapter_executable,
                    )
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
    except (StorageError, RegistryError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": type(error).__name__}))
        return 1
    except KeyboardInterrupt:
        return 130
