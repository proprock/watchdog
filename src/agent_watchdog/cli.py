import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from agent_watchdog import daemon
from agent_watchdog.config import (
    ConfigError,
    Limits,
    Project,
    UserPaths,
    load_config,
    project_aliases,
    project_for_reference,
    user_paths,
)
from agent_watchdog.diagnostics import Level, emit, error_code
from agent_watchdog.hook_install import change
from agent_watchdog.hooks import observe
from agent_watchdog.registry import RegistryError
from agent_watchdog.storage import StorageError


def _log(
    paths: UserPaths,
    level: Level,
    *,
    event: str,
    decision: str,
    error_type: str | None = None,
    detail: str | None = None,
) -> None:
    try:
        limits = load_config(paths.config).defaults
    except ConfigError:
        limits = Limits()
    emit(
        paths.data,
        limits,
        level,
        component="cli",
        event=event,
        decision=decision,
        error_type=error_type,
        detail=detail,
    )


def _project_record(project: Project, aliases: dict[UUID, str]) -> dict:
    record = project.model_dump(mode="json")
    record.pop("id")
    return {"project": aliases[project.id], **record}


def _with_project_alias(result: dict, alias: str) -> dict:
    result.pop("project_id", None)
    return {"project": alias, **result}


def _since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--since must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--since must include a UTC offset")
    return parsed


def _days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--days must be a whole number") from error
    if days < 1:
        raise argparse.ArgumentTypeError("--days must be at least 1")
    return days


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
    hook = commands.add_parser(
        "hook",
        help="Observe one hook event from stdin (developer/test utility, not installed "
        "in production by 'hooks install')",
    )
    hook.add_argument("provider", choices=("codex", "claude"))
    hook.add_argument("--installation", help=argparse.SUPPRESS)
    hooks = commands.add_parser("hooks", help="Preview or apply a Codex or Claude hook edit")
    hooks.add_argument("action", choices=("install", "uninstall"))
    hooks.add_argument("provider", choices=("codex", "claude"))
    hooks.add_argument("--file", type=Path, required=True)
    hooks.add_argument(
        "--adapter-executable",
        type=Path,
        help="Absolute native adapter path (required for install)",
    )
    hooks.add_argument(
        "--adapter-artifact",
        type=Path,
        help="Absolute packaged native adapter ZIP for this host (required for install)",
    )
    hooks.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    projects = commands.add_parser("project", help="Manage explicitly registered projects")
    actions = projects.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    actions.add_parser("add").add_argument("path", type=Path)
    actions.add_parser("remove").add_argument("project")
    relocate = actions.add_parser("relocate")
    relocate.add_argument("project")
    relocate.add_argument("path", type=Path)
    commands.add_parser("doctor", help="Inspect configuration, storage, and daemon health")
    readiness = commands.add_parser(
        "readiness", help="Check per stage whether provider events reach the project store"
    )
    readiness.add_argument("provider", choices=("codex", "claude"))
    readiness.add_argument("--file", type=Path, required=True, help="Installed hook file to check")
    readiness.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    readiness.add_argument(
        "--probe",
        action="store_true",
        help="Send one labeled synthetic event through the installed adapter, then purge it",
    )
    commands.add_parser("summary", help="Summarize known observations for registered projects")
    report = commands.add_parser(
        "report", help="Analyze collected observations without control actions"
    )
    report.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    report.add_argument("--session", help="Limit the report to one native session")
    report.add_argument("--provider", default="codex")
    telemetry = commands.add_parser(
        "telemetry", help="Report read-only delivery-pipeline measurements"
    )
    telemetry.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    telemetry.add_argument("--since", type=_since, help="ISO-8601 inclusive lower bound")
    usage = commands.add_parser(
        "usage", help="Compare token use and per-turn process cost (read-only, schema v6)"
    )
    usage.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    usage.add_argument("--since", type=_since, help="ISO-8601 inclusive lower bound")
    usage.add_argument("--until", type=_since, help="ISO-8601 exclusive upper bound")
    usage.add_argument(
        "--tariffs",
        type=Path,
        help="List-price tariff file; adds an estimate block. Defaults to [pricing] tariffs",
    )
    analyze = commands.add_parser(
        "analyze", help="Analysis subcommands over collected observations (read-only)"
    )
    analyze_actions = analyze.add_subparsers(dest="action", required=True)
    errors_view = analyze_actions.add_parser(
        "errors", help="List individual failing tool-call events"
    )
    errors_view.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    errors_view.add_argument(
        "--provider", help="Limit to one provider namespace; default scans every provider"
    )
    errors_view.add_argument("--session", help="Limit to one native session (requires --provider)")
    errors_view.add_argument("--since", type=_since, help="ISO-8601 inclusive lower bound")
    errors_view.add_argument("--until", type=_since, help="ISO-8601 exclusive upper bound")
    errors_view.add_argument("--limit", type=int, default=100)
    errors_view.add_argument("--offset", type=int, default=0)
    insights_view = commands.add_parser(
        "insights",
        help="Ask one isolated `claude -p` call for recommendations over collected evidence",
    )
    insights_view.add_argument(
        "mode",
        choices=(
            "errors",
            "context",
            "tokens",
            "workflow",
            "subagents",
            "permissions",
            "session",
            "sessions",
            "digest",
        ),
    )
    scope_group = insights_view.add_mutually_exclusive_group()
    scope_group.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    scope_group.add_argument(
        "--all-projects",
        action="store_true",
        help="Analyze every registered project that allows insights together "
        "(errors, permissions, workflow)",
    )
    insights_view.add_argument(
        "--provider", help="Limit to one provider namespace; default scans every provider"
    )
    insights_view.add_argument(
        "--session", help="Limit to one native session (requires --provider)"
    )
    window = insights_view.add_mutually_exclusive_group()
    window.add_argument(
        "--since", type=_since, help="ISO-8601 inclusive lower bound; default 7 days ago"
    )
    window.add_argument("--days", type=_days, help="Look back this many days instead of --since")
    insights_view.add_argument("--until", type=_since, help="ISO-8601 exclusive upper bound")
    insights_view.add_argument("--model", default="sonnet", help="Claude model alias or name")
    insights_view.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    insights_view.add_argument(
        "--timeout", type=float, default=600.0, help="Seconds before the call is abandoned"
    )
    insights_view.add_argument(
        "--max-bundle-tokens",
        type=int,
        help="Estimated token budget for the evidence bundle; default scales with the "
        "context window the model reported last time (120000 before the first call)",
    )
    insights_view.add_argument(
        "--language", default="English", help="Language for summary and recommendation prose"
    )
    insights_view.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the redacted evidence bundle and send nothing",
    )
    insights_view.add_argument(
        "--output", type=Path, help="Also write a Markdown report; never overwrites"
    )
    label = commands.add_parser("label", help="Label one collected session through the core")
    label.add_argument("session_id")
    label.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    label.add_argument("--provider", default="codex")
    label.add_argument(
        "--outcome", required=True, choices=("success", "partial", "failed", "abandoned", "unknown")
    )
    label.add_argument("--task-type")
    label.add_argument(
        "--progress",
        choices=("progress", "slow", "stuck", "externally_blocked"),
        help="Manual progress judgement; omitting it keeps a previously recorded state",
    )
    label.add_argument("--note", help="Reviewer note; omitting it keeps a previous note")
    verdict = commands.add_parser(
        "verdict", help="Record a manual correctness verdict for one shadow finding"
    )
    verdict.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    verdict.add_argument("--provider", default="codex")
    scope = verdict.add_mutually_exclusive_group(required=True)
    scope.add_argument("--session", help="Session-scoped finding")
    scope.add_argument("--checkout", help="Checkout-scoped finding such as a diff oscillation")
    verdict.add_argument("--rule", required=True)
    verdict.add_argument("--rule-version", required=True)
    verdict.add_argument(
        "--fingerprint", required=True, help="Finding fingerprint reported by `report`"
    )
    verdict.add_argument(
        "--verdict", required=True, choices=("true_positive", "false_positive", "uncertain")
    )
    verdict.add_argument("--note")
    pin = commands.add_parser("pin", help="Pin or unpin one collected session through the core")
    pin.add_argument("session_id")
    pin.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    pin.add_argument("--provider", default="codex")
    pin.add_argument("--unpin", action="store_true")
    purge = commands.add_parser("purge", help="Permanently delete Watchdog data for one session")
    purge.add_argument("session_id")
    purge.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    purge.add_argument("--provider", default="codex")
    export = commands.add_parser(
        "export", help="Write an offline review bundle for selected sessions"
    )
    export.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
    export.add_argument("--provider", default="codex")
    export.add_argument("--session", action="append", required=True)
    export.add_argument("--output", type=Path, required=True)
    sessions = commands.add_parser(
        "sessions", help="Read collected sessions without starting collection"
    )
    views = sessions.add_subparsers(dest="action", required=True)
    for action in ("list", "show"):
        view = views.add_parser(action)
        view.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")
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
    _log(paths, "DEBUG", event="command", decision="received")
    try:
        if args.command == "project":
            if args.action == "list":
                config = load_config(paths.config)
                aliases = project_aliases(config.projects)
                result = {
                    "projects": [_project_record(project, aliases) for project in config.projects]
                }
            else:
                from agent_watchdog.registry import Registry

                def mutate(registry: Registry) -> None:
                    nonlocal result
                    if args.action == "add":
                        project = registry.add(args.path)
                        result = _project_record(project, project_aliases(registry.config.projects))
                    elif args.action == "relocate":
                        project = project_for_reference(registry.config.projects, args.project)
                        if project is None:
                            raise RegistryError("Unknown project alias or UUID")
                        relocated = registry.relocate(project.id, args.path)
                        result = _project_record(
                            relocated, project_aliases(registry.config.projects)
                        )
                    else:
                        project = project_for_reference(registry.config.projects, args.project)
                        if project is None:
                            raise RegistryError("Unknown project alias or UUID")
                        alias = project_aliases(registry.config.projects)[project.id]
                        registry.remove(project.id)
                        result = {"removed": alias, "data_deleted": False}

                result = {}
                daemon.mutate_registry(paths, mutate)
            print(json.dumps(result))
            return 0
        if args.command == "readiness":
            from agent_watchdog import readiness

            report = readiness.check(
                paths, args.provider, args.file, project_ref=args.project, probe=args.probe
            )
            print(json.dumps(report))
            return 0 if report["ok"] else 1
        if args.command == "insights":
            from agent_watchdog import insights, inspection
            from agent_watchdog.insights import llm

            config = load_config(paths.config)
            roots = [item.root for item in config.projects]
            since = args.since
            if args.days is not None:
                since = datetime.now(UTC) - timedelta(days=args.days)
            options = {
                "mode": args.mode,
                "provider": args.provider,
                "since": since,
                "until": args.until,
                "model": args.model,
                "effort": args.effort,
                "timeout": args.timeout,
                "max_bundle_tokens": args.max_bundle_tokens,
                "language": args.language,
                "dry_run": args.dry_run,
                "output": args.output,
                "runner": lambda request: llm.claude(request, forbidden_roots=roots),
            }
            if args.all_projects:
                if args.session is not None:
                    raise StorageError("--all-projects cannot be combined with --session")
                result = insights.run_all(paths, **options)
            else:
                project = inspection.project_at(paths, args.project)
                result = insights.run(
                    paths,
                    project,
                    alias=project_aliases(config.projects)[project.id],
                    session_id=args.session,
                    **options,
                )
            print(json.dumps(result))
            return 1 if result["status"] == "unavailable" else 0
        if args.command in (
            "doctor",
            "summary",
            "sessions",
            "report",
            "telemetry",
            "usage",
            "analyze",
            "export",
            "label",
            "verdict",
            "pin",
            "purge",
        ):
            from agent_watchdog import inspection

            if args.command == "doctor":
                report = inspection.doctor(paths)
                print(json.dumps(report))
                return 0 if report["ok"] else 1
            if args.command == "summary":
                report = inspection.summary(paths)
                print(json.dumps(report))
                return 0 if report["ok"] else 1
            project = inspection.project_at(paths, args.project)
            alias = project_aliases(load_config(paths.config).projects)[project.id]
            if args.command == "export":
                result = inspection.export_sessions(
                    paths,
                    project,
                    provider=args.provider,
                    session_ids=args.session,
                    output=args.output,
                )
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command == "verdict":
                request = {
                    "action": "checkout_verdict" if args.checkout else "verdict",
                    "project_id": str(project.id),
                    "rule": args.rule,
                    "rule_version": args.rule_version,
                    "fingerprint": args.fingerprint,
                    "verdict": args.verdict,
                    "note": args.note,
                }
                if args.checkout:
                    request["checkout_id"] = args.checkout
                else:
                    request |= {"provider": args.provider, "session_id": args.session}
                result = daemon.request_control(paths, request)
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command in ("label", "pin", "purge"):
                request = {
                    "action": args.command,
                    "project_id": str(project.id),
                    "provider": args.provider,
                    "session_id": args.session_id,
                }
                if args.command == "label":
                    request |= {
                        "outcome": args.outcome,
                        "task_type": args.task_type,
                        "progress_state": args.progress,
                        "reviewer_note": args.note,
                    }
                elif args.command == "pin":
                    request["pinned"] = not args.unpin
                result = daemon.request_control(paths, request)
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command == "report":
                result = inspection.report(
                    paths, project, session_id=args.session, provider=args.provider
                )
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command == "telemetry":
                result = inspection.telemetry(paths, project, since=args.since)
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command == "usage":
                tariffs = args.tariffs or load_config(paths.config).pricing.tariffs
                result = inspection.usage(
                    paths, project, since=args.since, until=args.until, tariffs=tariffs
                )
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
            if args.command == "analyze":
                if args.action == "errors":
                    if args.session is not None and args.provider is None:
                        raise StorageError("Select --provider with --session")
                    result = inspection.errors(
                        paths,
                        project,
                        provider=args.provider,
                        session_id=args.session,
                        since=args.since,
                        until=args.until,
                        limit=args.limit,
                        offset=args.offset,
                    )
                print(json.dumps(_with_project_alias(result, alias)))
                return 0
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
            print(json.dumps(_with_project_alias(result, alias)))
            return 0
        if args.command == "hook":
            try:
                observe(paths, sys.stdin.buffer, args.provider)
            except KeyboardInterrupt:
                pass
            finally:
                # Claude adds hook stdout to model context on SessionStart and
                # UserPromptSubmit; an observation hook must stay silent there.
                if args.provider == "codex":
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
                        adapter_artifact=args.adapter_artifact,
                        provider=args.provider,
                        require_adapter=True,
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
        _log(
            paths,
            "WARNING",
            event="command",
            decision="failed",
            error_type=error_code(error),
            detail=str(error),
        )
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        _log(
            paths,
            "ERROR",
            event="command",
            decision="failed",
            error_type=error_code(error),
            detail=str(error),
        )
        print(json.dumps({"error": type(error).__name__}))
        return 1
    except KeyboardInterrupt:
        _log(paths, "INFO", event="command", decision="interrupted")
        return 130
