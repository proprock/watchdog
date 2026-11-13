"""wdi CLI: Typer application entry point."""

from __future__ import annotations

import io
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Never

import typer
from rich.console import Console

from wdi.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    Settings,
    create_default_config,
    load_settings,
)

if TYPE_CHECKING:
    from wdi.ingest.models import ImportStats

app = typer.Typer(
    no_args_is_help=True,
    help="watchdog-insights: local analytics for Claude Code and Codex sessions.",
)
config_app = typer.Typer(no_args_is_help=True, help="Configuration management.")
app.add_typer(config_app, name="config")

console = Console()
err_console = Console(stderr=True)


@config_app.command("init")
def config_init(
    path: Annotated[
        Path, typer.Option("--path", "-p", help="Path of the config file to create.")
    ] = DEFAULT_CONFIG_PATH,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Overwrite an existing config.")
    ] = False,
) -> None:
    """Create the default config and generate a machine_id."""
    try:
        settings = create_default_config(path, force=force)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Config created: {path.expanduser()}")
    console.print(f"machine_id: {settings.machine_id}")


def _load_settings_or_exit(config_path: Path | None) -> Settings:
    try:
        return load_settings(config_path)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _render_import_results(results: dict[str, ImportStats]) -> None:
    from rich.table import Table

    if not results:
        console.print("No enabled sources with existing directories.")
        return
    table = Table(title="Session import")
    table.add_column("Source")
    table.add_column("Files", justify="right")
    table.add_column("Imported", justify="right")
    table.add_column("New sessions", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Tool calls", justify="right")
    table.add_column("Subagents", justify="right")
    table.add_column("Warnings", justify="right")
    for source, stats in results.items():
        table.add_row(
            source,
            str(stats.files_seen),
            str(stats.files_imported),
            str(stats.sessions),
            str(stats.messages),
            str(stats.tool_calls),
            str(stats.subagent_runs),
            str(stats.warnings),
        )
    console.print(table)


def _run_import_with_progress(settings: Settings, *, full: bool) -> dict[str, ImportStats]:
    from rich.progress import Progress, TaskID

    from wdi.db.connection import SchemaRevisionError
    from wdi.ingest.models import ImportProgress
    from wdi.ingest.service import run_import

    source_tasks: dict[str, TaskID] = {}
    source_done: dict[str, int] = {}
    overall_total = 0

    with Progress(*_progress_columns(), console=console) as progress:
        overall = progress.add_task("[bold]Overall[/bold]", total=0)

        def _cb(event: ImportProgress) -> None:
            nonlocal overall_total
            task = source_tasks.get(event.source)
            if task is None:
                task = progress.add_task(f"[cyan]{event.source}", total=event.files_total)
                source_tasks[event.source] = task
                source_done[event.source] = 0
                overall_total += event.files_total
                progress.update(overall, total=overall_total)

            previous_done = source_done[event.source]
            source_done[event.source] = event.files_done
            progress.update(task, completed=event.files_done, total=event.files_total)
            if event.files_done > previous_done:
                progress.advance(overall, event.files_done - previous_done)

        try:
            return run_import(settings, full=full, on_progress=_cb)
        except SchemaRevisionError as exc:
            _exit_schema_error(exc)


def _progress_columns():  # noqa: ANN202 - rich column tuple, kept local to CLI rendering
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    return (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description:<22}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )


@app.command("init")
def init_cmd(
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="Config file to create or use.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Recreate the config if it already exists.")
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Re-read all files during the import.")
    ] = False,
) -> None:
    """One-step setup: create the config (if missing) and import all sources."""
    target = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    if force or not target.is_file():
        try:
            settings = create_default_config(config_path or DEFAULT_CONFIG_PATH, force=force)
        except ConfigError as exc:
            err_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"Config created: {target}")
        console.print(f"machine_id: {settings.machine_id}")
    else:
        settings = _load_settings_or_exit(config_path)
        console.print(f"Using existing config: {target}")

    _render_import_results(_run_import_with_progress(settings, full=full))


@app.command("import")
def import_cmd(
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to the config file.")
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", "-f", help="Re-read all files from scratch.")
    ] = False,
) -> None:
    """Import sessions from all enabled sources (incrementally)."""
    settings = _load_settings_or_exit(config_path)
    _render_import_results(_run_import_with_progress(settings, full=full))


# Reusable option annotations for the read commands.
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Path to the config file.")]
ProjectOpt = Annotated[
    str | None, typer.Option("--project", "-p", help="Filter by canonical project name.")
]
AgentOpt = Annotated[
    str | None, typer.Option("--agent", "-a", help="Filter by agent: claude or codex.")
]
SinceOpt = Annotated[
    str | None, typer.Option("--since", "-s", help="Only sessions since DATE (ISO or e.g. 7d).")
]
IncludeNoiseOpt = Annotated[
    bool,
    typer.Option(
        "--include-noise",
        help="Include background sessions (headless sdk-cli one-shots) in results.",
    ),
]


def _exit_schema_error(exc: RuntimeError) -> Never:
    err_console.print(f"Error: {exc}", markup=False)
    raise typer.Exit(code=1) from exc


def _open_db(settings: Settings):  # noqa: ANN202 - sqlite3.Connection, kept local
    from wdi.db.connection import SchemaRevisionError, connect

    try:
        return connect(settings.db_path)
    except SchemaRevisionError as exc:
        _exit_schema_error(exc)


def _print_hook_reconciliation(conn) -> None:  # noqa: ANN001
    from wdi.metrics.hooks import reconcile_tokens

    rows = reconcile_tokens(conn)
    console.print("Token reconciliation (difference threshold: >1% of Watchdog)")
    totals = {}
    for row in rows:
        key = (row["provider"], row["scope"], row["metric"])
        group = totals.setdefault(key, {"wdi": [], "watchdog": [], "unknown": 0})
        if row["differs"] is not None:
            for source in ("wdi", "watchdog"):
                group[source].append(row[source])
        group["unknown"] += row["differs"] is None
    if not rows:
        console.print("  No sessions with both transcript and Watchdog usage.")
    for (provider, scope, metric), group in sorted(totals.items()):
        values = {
            source: sum(group[source]) if group[source] else "unavailable"
            for source in ("wdi", "watchdog")
        }
        console.print(
            f"  Total {provider}/{scope}/{metric}: "
            f"wdi={values['wdi']} watchdog={values['watchdog']} "
            f"unavailable_pairs={group['unknown']}",
            markup=False,
        )
    for row in rows:
        if row["differs"]:
            console.print(
                f"  Difference {row['session_id']} ({row['machine_id']}) "
                f"{row['provider']}/{row['scope']}/{row['metric']}: "
                f"wdi={row['wdi']} watchdog={row['watchdog']}",
                markup=False,
            )


@app.command("doctor")
def doctor_cmd(config_path: ConfigOpt = None) -> None:
    """Report Watchdog stores and transcript source coverage."""
    from wdi.db.repository import Repository
    from wdi.ingest.models import MAIN_STREAM
    from wdi.ingest.watchdog import scan_stores

    settings = _load_settings_or_exit(config_path)
    captured = set()
    for scan in scan_stores(settings.watchdog.data_dirs):
        console.print(f"Watchdog data directory: {scan.data_dir}", markup=False, soft_wrap=True)
        console.print(f"  Stores found: {len(scan.stores)}")
        for store in scan.stores:
            status = "supported" if store.supported else "unsupported"
            console.print(
                f"  {store.path.name} (contract {store.contract}, {status})", markup=False
            )
        for warning in scan.warnings:
            err_console.print(f"Warning: {warning}", markup=False)
        captured.update(
            (source.stream.provider, source.stream.session_id)
            for source in scan.sources
            if source.record_count and source.stream.stream_id == MAIN_STREAM
        )

    rows = []
    if settings.db_path.is_file():
        with closing(_open_db(settings)) as conn:
            rows = Repository(conn).session_coverage()
            _print_hook_reconciliation(conn)
    origins = Counter(row["origin"] for row in rows)
    file_only = Counter(
        row["agent"]
        for row in rows
        if row["origin"] == "file"
        and row["source_path"] is not None
        and (row["agent"], row["session_id"]) not in captured
    )
    console.print(
        f"Sessions by origin: file={origins['file']}, watchdog={origins['watchdog']}, "
        f"legacy={origins['legacy']}"
    )
    console.print("File-only sessions by provider")
    for agent in ("claude", "codex"):
        console.print(f"  {agent}: {file_only[agent]}")


def _make_filter(
    agent: str | None,
    project: str | None,
    since: str | None,
    include_noise: bool = False,
):  # noqa: ANN202
    from wdi.metrics.filters import SessionFilter

    return SessionFilter(agent=agent, project=project, since=since, include_noise=include_noise)


@app.command("stats")
def stats_cmd(
    config_path: ConfigOpt = None,
    project: ProjectOpt = None,
    agent: AgentOpt = None,
    since: SinceOpt = None,
    include_noise: IncludeNoiseOpt = False,
) -> None:
    """Print quantitative metrics as terminal tables."""
    from rich.table import Table

    from wdi.cli_format import fmt_cost, fmt_duration, fmt_int, fmt_pct, fmt_tokens
    from wdi.metrics.compute import build_report
    from wdi.metrics.pricing import PricingResolver

    settings = _load_settings_or_exit(config_path)
    flt = _make_filter(agent, project, since, include_noise)
    conn = _open_db(settings)
    try:
        report = build_report(conn, flt, PricingResolver(settings.pricing))
    finally:
        conn.close()

    ov = report.overview
    if ov.sessions == 0:
        console.print("No sessions match the filter.")
        return

    av = report.availability
    summary = Table(title="Overview", show_header=False)
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row(
        "Sessions", f"{ov.sessions} (claude {ov.claude_sessions}, codex {ov.codex_sessions})"
    )
    summary.add_row("Projects", fmt_int(ov.projects))
    summary.add_row(
        "Input / output tokens", f"{fmt_tokens(ov.input_tokens)} / {fmt_tokens(ov.output_tokens)}"
    )
    summary.add_row("Cache read tokens", fmt_tokens(ov.cache_read_tokens))
    summary.add_row("Cache hit rate", fmt_pct(ov.cache_hit_rate))
    if av.cache_creation:
        summary.add_row("Cache creation tokens (claude only)", fmt_tokens(ov.cache_creation_tokens))
    if av.reasoning:
        summary.add_row("Reasoning tokens (codex only)", fmt_tokens(ov.reasoning_tokens))
    summary.add_row("Estimated cost", fmt_cost(ov.estimated_cost))
    summary.add_row("Tool calls", fmt_int(ov.tool_calls))
    summary.add_row("Total agent time", fmt_duration(ov.total_duration_ms))
    if av.ttft and ov.avg_time_to_first_token_ms is not None:
        summary.add_row("Avg TTFT (codex only)", fmt_duration(int(ov.avg_time_to_first_token_ms)))
    if av.context_window and ov.max_context_pressure is not None:
        summary.add_row("Peak context pressure (codex only)", fmt_pct(ov.max_context_pressure))
    if ov.turns:
        summary.add_row(
            "Turns / interrupted (codex only)",
            f"{fmt_int(ov.turns)} / {fmt_int(ov.turn_aborts)}",
        )
    if av.rate_limits:
        summary.add_row(
            "Rate limit used (codex only)",
            f"{fmt_pct((ov.rate_limit_primary_max or 0) / 100)}"
            f" / {fmt_pct((ov.rate_limit_secondary_max or 0) / 100)}",
        )
    summary.add_row("Compactions / API errors", f"{ov.compactions} / {ov.api_errors}")
    console.print(summary)

    if report.unpriced_models:
        warning = "; ".join(
            f"{m['model']} ({fmt_tokens(int(m['tokens']))})" for m in report.unpriced_models
        )
        console.print(
            f"[yellow]Unpriced models: {warning} - cost totals are underestimated.[/yellow]"
        )

    if report.models:
        models = Table(title="Tokens and cost by model")
        models.add_column("Model")
        models.add_column("Sessions", justify="right")
        models.add_column("Input", justify="right")
        models.add_column("Output", justify="right")
        models.add_column("Reasoning", justify="right")
        models.add_column("Cost", justify="right")
        for m in report.models:
            models.add_row(
                m.model,
                fmt_int(m.sessions),
                fmt_tokens(m.input_tokens),
                fmt_tokens(m.output_tokens),
                fmt_tokens(m.reasoning_tokens),
                fmt_cost(m.cost),
            )
        console.print(models)

    if report.tool_categories:
        tools = Table(title="Tool usage")
        tools.add_column("Category")
        tools.add_column("Calls", justify="right")
        tools.add_column("Errors", justify="right")
        tools.add_column("Error rate", justify="right")
        for t in report.tool_categories:
            tools.add_row(t.category, fmt_int(t.calls), fmt_int(t.errors), fmt_pct(t.error_rate))
        console.print(tools)

    if report.tool_details:
        detail = Table(title="Tool detail")
        detail.add_column("Tool")
        detail.add_column("Calls", justify="right")
        detail.add_column("Errors", justify="right")
        detail.add_column("Err rate", justify="right")
        detail.add_column("p50", justify="right")
        detail.add_column("p95", justify="right")
        for t in report.tool_details:
            detail.add_row(
                t.tool_name,
                fmt_int(t.calls),
                fmt_int(t.errors),
                fmt_pct(t.error_rate),
                fmt_duration(t.p50_ms),
                fmt_duration(t.p95_ms),
            )
        console.print(detail)

    if report.shell_failure_clusters:
        clusters = Table(title="Shell failure clusters")
        clusters.add_column("Command kind")
        clusters.add_column("Calls", justify="right")
        clusters.add_column("Errors", justify="right")
        for c in report.shell_failure_clusters:
            clusters.add_row(c.command_kind, fmt_int(c.calls), fmt_int(c.errors))
        console.print(clusters)

    if report.waste:
        waste = Table(title="Waste signals", show_header=False)
        waste.add_column("Metric")
        waste.add_column("Value", justify="right")
        waste.add_row("Repeated reads", fmt_int(report.waste.repeated_reads))
        waste.add_row("Duplicate commands", fmt_int(report.waste.duplicate_commands))
        waste.add_row(
            "Failed tool calls",
            f"{fmt_int(report.waste.failed_calls)}"
            f" ({fmt_int(report.waste.failed_output_bytes)} B output)",
        )
        if av.stop_reason:
            waste.add_row(
                "Truncated outputs (claude only)", fmt_int(report.waste.truncated_outputs)
            )
        if av.turn_aborts:
            waste.add_row("Interrupted turns (codex only)", fmt_int(report.waste.aborted_turns))
        if report.cache_misses is not None and report.cache_misses.total:
            m = report.cache_misses
            waste.add_row(
                "Cache misses (claude only, cold/tool/idle/other)",
                f"{m.cold_start} / {m.tool_wait} / {m.user_idle} / {m.other}",
            )
            waste.add_row("Cache miss wasted tokens", fmt_tokens(m.wasted_tokens))
            waste.add_row("Cache miss cost", fmt_cost(m.estimated_cost))
        console.print(waste)

    if report.hooks is not None and report.hooks.hook_count:
        hooks = Table(title="Hooks (claude only)")
        hooks.add_column("Command")
        hooks.add_column("Runs", justify="right")
        for command, count in report.hooks.top_commands:
            hooks.add_row(command, fmt_int(count))
        hooks.add_row(
            "(total)",
            f"{fmt_int(report.hooks.hook_count)}"
            f" in {fmt_int(report.hooks.sessions_with_hooks)} sessions,"
            f" {fmt_int(report.hooks.hook_error_count)} errors",
        )
        console.print(hooks)

    if report.projects:
        projects = Table(title="Projects")
        projects.add_column("Project")
        projects.add_column("Sessions", justify="right")
        projects.add_column("Tokens", justify="right")
        projects.add_column("Tool calls", justify="right")
        for p in report.projects:
            projects.add_row(
                p.project, fmt_int(p.sessions), fmt_tokens(p.tokens), fmt_int(p.tool_calls)
            )
        console.print(projects)


sessions_app = typer.Typer(invoke_without_command=True, help="Inspect imported sessions.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.callback()
def sessions_main(
    ctx: typer.Context,
    config_path: ConfigOpt = None,
    project: ProjectOpt = None,
    agent: AgentOpt = None,
    since: SinceOpt = None,
    include_noise: IncludeNoiseOpt = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows.")] = 20,
) -> None:
    """List recent sessions when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    from rich.table import Table

    from wdi.cli_format import display_name, fmt_duration, fmt_tokens
    from wdi.metrics import queries

    settings = _load_settings_or_exit(config_path)
    flt = _make_filter(agent, project, since, include_noise)
    conn = _open_db(settings)
    try:
        rows = queries.session_list(conn, flt, limit)
    finally:
        conn.close()

    if not rows:
        console.print("No sessions match the filter.")
        return

    table = Table(title="Recent sessions")
    table.add_column("Id", overflow="fold")
    table.add_column("Name", overflow="fold")
    table.add_column("Agent")
    table.add_column("Project")
    table.add_column("Started")
    table.add_column("Tokens", justify="right")
    table.add_column("Duration", justify="right")
    for r in rows:
        table.add_row(
            r["session_id"][:8],
            display_name(r),
            r["agent"],
            r["project"],
            (r["started_at"] or "")[:16],
            fmt_tokens((r["input_tokens"] or 0) + (r["output_tokens"] or 0)),
            fmt_duration(r["duration_ms"]),
        )
    console.print(table)


@sessions_app.command("show")
def sessions_show(
    session_id: Annotated[str, typer.Argument(help="Session id (full or unique prefix).")],
    config_path: ConfigOpt = None,
) -> None:
    """Show one session in detail."""
    from rich.table import Table

    from wdi.cli_format import display_name, fmt_cost, fmt_duration, fmt_int, fmt_tokens
    from wdi.metrics import queries
    from wdi.metrics.pricing import PricingResolver

    settings = _load_settings_or_exit(config_path)
    conn = _open_db(settings)
    try:
        row = queries.session_detail(conn, session_id)
        if row is None:
            # Allow a unique id prefix for convenience.
            matches = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 2",
                (session_id + "%",),
            ).fetchall()
            if len(matches) == 1:
                row = queries.session_detail(conn, matches[0]["session_id"])
        if row is None:
            err_console.print(f"[red]Error:[/red] session not found: {session_id}")
            raise typer.Exit(code=1)
        categories = queries.session_tool_categories(conn, row["session_id"])
        subagents = queries.session_subagents(conn, row["session_id"])
        from wdi.metrics.hooks import session_hook_metrics

        hook_metrics = session_hook_metrics(conn, row["session_id"])
    finally:
        conn.close()

    pricing = PricingResolver(settings.pricing)
    cost = pricing.cost(
        row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_creation_tokens=row["cache_creation_tokens"] or 0,
    )

    detail = Table(title=display_name(row), show_header=False)
    detail.add_column("Field")
    detail.add_column("Value", overflow="fold")
    detail.add_row("Session id", row["session_id"])
    detail.add_row("Agent / model", f"{row['agent']} / {row['model'] or '—'}")
    detail.add_row("Project", row["project"])
    detail.add_row("Git branch", row["git_branch"] or "—")
    detail.add_row("Started / ended", f"{row['started_at'] or '—'} → {row['ended_at'] or '—'}")
    detail.add_row("Duration", fmt_duration(row["duration_ms"]))
    detail.add_row(
        "User / assistant messages", f"{row['user_msg_count']} / {row['assistant_msg_count']}"
    )
    detail.add_row(
        "Input / output tokens",
        f"{fmt_tokens(row['input_tokens'])} / {fmt_tokens(row['output_tokens'])}",
    )
    detail.add_row("Cache read tokens", fmt_tokens(row["cache_read_tokens"]))
    if row["reasoning_tokens"] is not None:
        detail.add_row("Reasoning tokens", fmt_tokens(row["reasoning_tokens"]))
    detail.add_row("Estimated cost", fmt_cost(cost))
    detail.add_row("Tool calls", fmt_int(row["tool_call_count"]))
    detail.add_row(
        "Compactions / API errors", f"{row['compaction_count']} / {row['api_error_count']}"
    )
    console.print(detail)

    console.print("Hook observations")
    wait = hook_metrics["permission_wait_ms"]
    console.print(f"  Permission wait: {fmt_duration(wait) if wait is not None else 'unavailable'}")
    console.print(
        f"  Permission prompts: {hook_metrics['permission_waits']}; "
        f"incomplete waits: {hook_metrics['incomplete_permission_waits']}"
    )
    for tool, duration in sorted(hook_metrics["tool_durations_ms"].items()):
        console.print(
            f"  Tool {tool}: {fmt_duration(duration) if duration is not None else 'unavailable'}",
            markup=False,
        )
    for control in hook_metrics["control_actions"]:
        console.print(
            f"  Delivered {control['action']} / {control['rule']}: {control['count']}",
            markup=False,
        )

    if categories:
        tools = Table(title="Tools")
        tools.add_column("Category")
        tools.add_column("Calls", justify="right")
        tools.add_column("Errors", justify="right")
        for c in categories:
            tools.add_row(c["category"], fmt_int(c["calls"]), fmt_int(c["errors"]))
        console.print(tools)

    if subagents:
        agents = Table(title="Subagents")
        agents.add_column("Type")
        agents.add_column("Status")
        agents.add_column("Tokens", justify="right")
        agents.add_column("Duration", justify="right")
        agents.add_column("Tool calls", justify="right")
        for a in subagents:
            agents.add_row(
                a["agent_type"] or "—",
                a["status"] or "—",
                fmt_tokens(a["total_tokens"]),
                fmt_duration(a["total_duration_ms"]),
                fmt_int(a["total_tool_use_count"]),
            )
        console.print(agents)


@app.command("agents")
def agents_cmd(
    config_path: ConfigOpt = None,
    project: ProjectOpt = None,
    agent: AgentOpt = None,
    since: SinceOpt = None,
    include_noise: IncludeNoiseOpt = False,
) -> None:
    """Summarize subagent efficiency by agent type (Claude traces, Codex threads)."""
    from rich.table import Table

    from wdi.cli_format import fmt_int, fmt_pct, fmt_ratio, fmt_tokens
    from wdi.metrics import queries

    settings = _load_settings_or_exit(config_path)
    flt = _make_filter(agent, project, since, include_noise)
    conn = _open_db(settings)
    try:
        rows = queries.subagent_types(conn, flt)
        opportunities = queries.subagent_opportunities(conn, flt)
    finally:
        conn.close()

    if not rows:
        console.print("No subagent runs match the filter.")
        return

    table = Table(title="Subagent efficiency by type")
    table.add_column("Type")
    table.add_column("Runs", justify="right")
    table.add_column("Avg tokens", justify="right")
    table.add_column("Total tokens", justify="right")
    table.add_column("Avg calls", justify="right")
    table.add_column("Search/Edit", justify="right")
    table.add_column("Repeated reads", justify="right")
    table.add_column("Trace err rate", justify="right")
    for r in rows:
        edit = r["edit_count"]
        search_to_edit = r["search_count"] / edit if edit else None
        trace_calls = r["trace_calls"]
        err_rate = r["trace_errors"] / trace_calls if trace_calls else None
        table.add_row(
            r["agent_type"],
            fmt_int(r["runs"]),
            fmt_tokens(int(r["avg_tokens"] or 0)),
            fmt_tokens(r["total_tokens"]),
            fmt_ratio(r["avg_tool_calls"]),
            fmt_ratio(search_to_edit),
            fmt_int(r["repeated_reads"]),
            fmt_pct(err_rate),
        )
    console.print(table)

    if opportunities:
        console.print(
            f"[yellow]{len(opportunities)}[/yellow] exploration-heavy session(s) ran no"
            " subagents - candidates for delegating discovery (see the web dashboard)."
        )


@app.command("analyze")
def analyze_cmd(
    config_path: ConfigOpt = None,
    analysis_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            "-t",
            help="Analyzer: prompt-quality, summary, agent-efficiency (all if omitted).",
        ),
    ] = None,
    project: ProjectOpt = None,
    agent: AgentOpt = None,
    since: SinceOpt = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Max sessions per analyzer.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Override the analysis model.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the cost confirmation prompt.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Re-analyze cached sessions, overwriting results."),
    ] = False,
    workers: Annotated[
        int,
        typer.Option("--workers", "-w", help="Parallel LLM calls per analyzer.", min=1),
    ] = 4,
    include_noise: IncludeNoiseOpt = False,
) -> None:
    """Run LLM analysis over sessions (cached by analyzer prompt version)."""
    from rich.table import Table

    from wdi.cli_format import fmt_cost, fmt_tokens
    from wdi.llm.backend import ClaudeCliBackend, CodexCliBackend, LLMBackend
    from wdi.llm.service import (
        AnalysisStats,
        estimate_analyzer,
        resolve_analyzers,
        run_analyzer,
    )
    from wdi.metrics.pricing import PricingResolver

    settings = _load_settings_or_exit(config_path)
    llm_cfg = settings.features.llm_analysis
    if not llm_cfg.enabled:
        console.print("LLM analysis is disabled in the config.")
        return
    try:
        analyzers = resolve_analyzers(analysis_type)
    except KeyError:
        err_console.print(f"[red]Error:[/red] unknown analyzer type: {analysis_type}")
        raise typer.Exit(code=1) from None

    per_run = limit if limit is not None else llm_cfg.max_sessions_per_run
    flt = _make_filter(agent, project, since, include_noise)
    pricing = PricingResolver(settings.pricing)
    conn = _open_db(settings)
    try:
        # Pre-flight: estimate how much will be sent to the LLM and confirm before
        # processing, in case a large batch would be expensive.
        plan = [
            (analyzer, model or llm_cfg.analysis_model_for(analyzer.analysis_type))
            for analyzer in analyzers
        ]
        from rich.progress import Progress

        estimates = []
        estimate_totals: dict[str, int] = {}
        estimate_done: dict[str, int] = {}
        estimate_overall_total = 0

        with Progress(*_progress_columns(), console=console) as progress:
            overall = progress.add_task("[bold]Estimating[/bold]", total=0)
            for analyzer, chosen in plan:
                atask = progress.add_task(f"[cyan]{analyzer.key}", total=0)

                def _estimate_cb(
                    done: int,
                    total: int,
                    *,
                    key: str = analyzer.key,
                    task: object = atask,
                ) -> None:
                    nonlocal estimate_overall_total
                    previous_total = estimate_totals.get(key, 0)
                    previous_done = estimate_done.get(key, 0)
                    estimate_totals[key] = total
                    estimate_done[key] = done
                    if total > previous_total:
                        estimate_overall_total += total - previous_total
                        progress.update(overall, total=estimate_overall_total)
                    # Preserve the existing progress callback typing.
                    progress.update(task, completed=done, total=total)  # ty: ignore[invalid-argument-type]
                    if done > previous_done:
                        progress.advance(overall, done - previous_done)

                estimates.append(
                    estimate_analyzer(
                        conn,
                        analyzer,
                        flt,
                        model=chosen,
                        limit=per_run,
                        pricing=pricing,
                        data_dirs=settings.watchdog.data_dirs,
                        force=force,
                        on_progress=_estimate_cb,
                    )
                )
        total_sessions = sum(e.sessions for e in estimates)
        if total_sessions == 0:
            console.print("Nothing to analyze: matching sessions are already cached.")
            return

        est_table = Table(title="Estimated processing (approximate)")
        est_table.add_column("Analyzer")
        est_table.add_column("Model")
        est_table.add_column("Sessions", justify="right")
        est_table.add_column("~Input tokens", justify="right")
        est_table.add_column("~Output tokens", justify="right")
        est_table.add_column("~Cost", justify="right")
        total_input = total_output = 0
        total_cost: float | None = None
        for e in estimates:
            total_input += e.input_tokens
            total_output += e.output_tokens
            if e.cost is not None:
                total_cost = (total_cost or 0.0) + e.cost
            est_table.add_row(
                e.analyzer,
                e.model,
                str(e.sessions),
                fmt_tokens(e.input_tokens),
                fmt_tokens(e.output_tokens),
                fmt_cost(e.cost),
            )
        est_table.add_section()
        est_table.add_row(
            "Total",
            "",
            str(total_sessions),
            fmt_tokens(total_input),
            fmt_tokens(total_output),
            fmt_cost(total_cost),
        )
        console.print(est_table)
        console.print(
            "[dim]Tokens are estimated (~4 chars/token); cost uses the configured "
            "pricing table.[/dim]"
        )

        if not yes and not typer.confirm("Proceed with analysis?"):
            console.print("Aborted.")
            return

        from rich.progress import Progress

        backend: LLMBackend
        if llm_cfg.backend == "codex":
            backend = CodexCliBackend(executable=llm_cfg.codex_executable, structured=False)
        else:
            backend = ClaudeCliBackend(data_dirs=settings.watchdog.data_dirs)
        all_stats: list[tuple[str, str, AnalysisStats]] = []

        with Progress(
            *_progress_columns(),
            console=console,
        ) as progress:
            overall = progress.add_task("[bold]Overall[/bold]", total=total_sessions)
            for (analyzer, chosen), estimate in zip(plan, estimates, strict=True):
                atask = progress.add_task(f"[cyan]{analyzer.key}", total=estimate.sessions)

                def _cb(
                    _ok: bool,
                    _at: object = atask,
                    _ot: object = overall,
                ) -> None:
                    # Preserve the existing progress callback typing.
                    progress.advance(_at)  # ty: ignore[invalid-argument-type]
                    # Preserve the existing progress callback typing.
                    progress.advance(_ot)  # ty: ignore[invalid-argument-type]

                stats = run_analyzer(
                    conn,
                    backend,
                    analyzer,
                    flt,
                    model=chosen,
                    limit=per_run,
                    force=force,
                    workers=workers,
                    data_dirs=settings.watchdog.data_dirs,
                    on_progress=_cb,
                )
                all_stats.append((analyzer.key, chosen, stats))

        result_table = Table(title="LLM analysis results")
        result_table.add_column("Analyzer")
        result_table.add_column("Model")
        result_table.add_column("Analyzed", justify="right")
        result_table.add_column("Skipped", justify="right")
        result_table.add_column("Failed", justify="right")
        for key, chosen, stats in all_stats:
            result_table.add_row(
                key,
                chosen,
                str(stats.analyzed),
                str(stats.skipped_not_applicable),
                str(stats.failed),
            )
            for err in stats.errors:
                err_console.print(f"warn {key}: {err}", markup=False)
        console.print(result_table)
        if any(stats.failed for _, _, stats in all_stats):
            raise typer.Exit(code=1)
    finally:
        conn.close()


@app.command("report")
def report_cmd(
    config_path: ConfigOpt = None,
    project: ProjectOpt = None,
    agent: AgentOpt = None,
    since: SinceOpt = None,
    include_noise: IncludeNoiseOpt = False,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Output HTML file path.")] = None,
) -> None:
    """Generate a self-contained static HTML report."""
    from wdi.metrics.compute import build_report
    from wdi.metrics.pricing import PricingResolver
    from wdi.report.html import render_report

    settings = _load_settings_or_exit(config_path)
    flt = _make_filter(agent, project, since, include_noise)
    conn = _open_db(settings)
    try:
        report = build_report(conn, flt, PricingResolver(settings.pricing))
    finally:
        conn.close()

    html = render_report(report)
    out_path = out or (settings.report.out_dir / "wdi-report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    console.print(f"Report written: {out_path} ({len(html):,} bytes)")


@app.command("export-snapshot")
def export_snapshot_cmd(
    config_path: ConfigOpt = None,
    out_dir: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Target directory (default: sync.export_dir)."),
    ] = None,
) -> None:
    """Export the database as a single-file snapshot for cross-machine sync."""
    from wdi.sync.snapshot import export_snapshot

    settings = _load_settings_or_exit(config_path)
    target = out_dir or settings.sync.export_dir
    if target is None:
        err_console.print(
            "[red]Error:[/red] no target directory. Pass --dir or set sync.export_dir."
        )
        raise typer.Exit(code=1)
    if not settings.db_path.is_file():
        err_console.print(f"[red]Error:[/red] database not found: {settings.db_path}")
        raise typer.Exit(code=1)
    path = export_snapshot(settings.db_path, target, settings.machine_id)
    console.print(f"Snapshot written: {path}")


@app.command("import-snapshot")
def import_snapshot_cmd(
    config_path: ConfigOpt = None,
    in_dir: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Source directory (default: sync.export_dir)."),
    ] = None,
) -> None:
    """Merge other machines' snapshots into the local database."""
    from wdi.sync.snapshot import import_snapshots

    settings = _load_settings_or_exit(config_path)
    source = in_dir or settings.sync.export_dir
    if source is None:
        err_console.print(
            "[red]Error:[/red] no source directory. Pass --dir or set sync.export_dir."
        )
        raise typer.Exit(code=1)
    if not source.is_dir():
        err_console.print(f"[red]Error:[/red] directory not found: {source}")
        raise typer.Exit(code=1)
    stats = import_snapshots(settings.db_path, source, settings.machine_id)
    console.print(
        f"Merged {stats.snapshots} snapshot(s): "
        f"+{stats.sessions} sessions, +{stats.messages} messages, "
        f"+{stats.tool_calls} tool calls, +{stats.subagent_runs} subagent runs, "
        f"+{stats.llm_analyses} analyses."
    )
    for skipped in stats.skipped:
        err_console.print(f"[yellow]warn[/yellow] {skipped}")


@app.command("serve")
def serve_cmd(
    config_path: ConfigOpt = None,
    host: Annotated[
        str | None, typer.Option("--host", "-h", help="Bind host (default: config server.host).")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port (default: config server.port).")
    ] = None,
    do_import: Annotated[
        bool, typer.Option("--import/--no-import", help="Run an incremental import before serving.")
    ] = False,
) -> None:
    """Run the local web server for browsing session analytics."""
    import uvicorn

    from wdi.web import create_app

    settings = _load_settings_or_exit(config_path)
    if do_import:
        from wdi.ingest.service import run_import

        _render_import_results(run_import(settings))

    bind_host = host or settings.server.host
    bind_port = port or settings.server.port
    console.print(f"Serving on http://{bind_host}:{bind_port} (Ctrl+C to stop)")
    uvicorn.run(create_app(settings), host=bind_host, port=bind_port, log_level="info")


@app.command("mcp")
def mcp_cmd(config_path: ConfigOpt = None) -> None:
    """Run the Model Context Protocol server over stdio."""
    from wdi.mcp import create_mcp_server

    settings = _load_settings_or_exit(config_path)
    create_mcp_server(settings).run()  # FastMCP defaults to stdio transport


def _ensure_unicode_output() -> None:
    # Legacy Windows console encodings (cp1250 etc.) cannot represent Cyrillic:
    # without errors="replace" any help output crashes with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and (stream.encoding or "").lower() not in (
            "utf-8",
            "utf8",
        ):
            stream.reconfigure(errors="replace")


def main() -> None:
    _ensure_unicode_output()
    app()
