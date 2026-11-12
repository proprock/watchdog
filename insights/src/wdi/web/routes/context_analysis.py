"""Context-analysis page and HTMX result fragment."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from wdi import context_analysis as analysis
from wdi.config import LlmAnalysisConfig
from wdi.context_economics import Scenario, compare_scenarios, estimate_prompt
from wdi.llm import context_advisor
from wdi.llm.backend import ClaudeCliBackend, CodexCliBackend, LLMError
from wdi.metrics import queries
from wdi.metrics.pricing import PricingResolver
from wdi.report.charts import embed_json
from wdi.web.dependencies import DbDep, PricingDep, SettingsDep, TemplatesDep

router = APIRouter()
SortKey = Literal[
    "model_calls",
    "turns",
    "input_tokens",
    "peak_context_tokens",
    "visible_context_bytes",
    "compactions",
    "repeated_bytes",
    "input_uncached",
    "cache_read",
    "cache_write",
    "output",
    "cost",
]
Provider = Literal["claude", "codex"]


def _query(flt: analysis.ContextAnalysisFilter, *, session_id: str | None = None) -> str:
    values: dict[str, str | int] = {
        "run": 1,
        "min_calls": flt.min_calls,
        "min_turns": flt.min_turns,
        "top_n": flt.top_n,
        "sort": flt.sort,
    }
    for key in ("agent", "project", "since", "until"):
        value = getattr(flt, key)
        if value:
            values[key] = value
    if flt.include_noise:
        values["include_noise"] = 1
    if session_id:
        values["session_id"] = session_id
    values["scope"] = flt.scope
    pairs = list(values.items()) + [("selected_ids", sid) for sid in flt.selected_ids]
    return urlencode(pairs)


def _chart_data(selected: analysis.SessionContextSummary | None) -> dict[str, object]:
    if selected is None:
        return {}
    categories = sorted(
        {category for snapshot in selected.snapshots for category in snapshot.categories}
    )
    return {
        "session": selected.session_id,
        "labels": [snapshot.call_index + 1 for snapshot in selected.snapshots],
        "categories": [
            {
                "label": category.replace("_", " ").title(),
                "values": [snapshot.categories.get(category, 0) for snapshot in selected.snapshots],
            }
            for category in categories
        ],
        "context_tokens": [snapshot.context_tokens for snapshot in selected.snapshots],
        "input_tokens": [snapshot.input_tokens for snapshot in selected.snapshots],
        "cache_read_tokens": [snapshot.cache_read_tokens for snapshot in selected.snapshots],
        "cache_creation_tokens": [
            snapshot.cache_creation_tokens for snapshot in selected.snapshots
        ],
        "utilization": [snapshot.utilization for snapshot in selected.snapshots],
        "compactions": [
            snapshot.generation > 0 and snapshot.compact_trigger is not None
            for snapshot in selected.snapshots
        ],
        "coverage": [snapshot.coverage for snapshot in selected.snapshots],
    }


def _advice_card(
    conn: sqlite3.Connection,
    flt: analysis.ContextAnalysisFilter,
    report: analysis.ContextAnalysisReport,
    config: LlmAnalysisConfig,
    provider: Provider,
    model: str | None = None,
    pricing: PricingResolver | None = None,
    output_budget: int = 600,
) -> dict[str, object]:
    options = list(
        config.coach_models.claude if provider == "claude" else config.coach_models.codex
    )
    if provider == "claude":
        options = list(dict.fromkeys([config.model_for("coach"), *options]))
    selected = model if model in options else options[0]
    card: dict[str, object] = {
        "provider": provider,
        "provider_label": "Claude" if provider == "claude" else "Codex",
        "models": options,
        "selected_model": selected,
        "advice": context_advisor.get_cached(
            conn, flt, report, backend_name=provider, model=selected
        ),
    }
    if pricing is not None:
        card["estimate"] = estimate_prompt(
            context_advisor.build_prompt(report), selected, output_budget, pricing
        )
    if model is not None and model not in options:
        card["error"] = "Choose a model from the configured list."
    return card


@router.get("/context-analysis", response_class=HTMLResponse)
def context_analysis_page(
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    settings: SettingsDep,
    pricing: PricingDep,
    run: Annotated[bool, Query()] = False,
    agent: Annotated[Literal["", "claude", "codex"] | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    include_noise: Annotated[bool, Query()] = False,
    min_calls: Annotated[int, Query(ge=1, le=100_000)] = 1,
    min_turns: Annotated[int, Query(ge=0, le=100_000)] = 0,
    top_n: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[SortKey, Query()] = "input_tokens",
    session_id: Annotated[str | None, Query()] = None,
    selected_ids: Annotated[list[str] | None, Query()] = None,
    scope: Annotated[Literal["filtered", "top", "selected"], Query()] = "filtered",
    model: Annotated[str | None, Query()] = None,
    estimate_provider: Annotated[Provider | None, Query()] = None,
    output_budget: Annotated[int, Query(ge=0, le=100000)] = 600,
) -> HTMLResponse:
    flt = analysis.ContextAnalysisFilter(
        agent=agent or None,
        project=project or None,
        since=since or None,
        until=until or None,
        include_noise=include_noise,
        min_calls=min_calls,
        min_turns=min_turns,
        top_n=top_n,
        sort=sort,
        session_id=session_id,
        selected_ids=tuple(selected_ids or []),
        scope=scope,
    )
    report = analysis.build_context_report(conn, flt, pricing) if run else None
    advice_cards = (
        [
            _advice_card(
                conn,
                flt,
                report,
                settings.features.llm_analysis,
                "claude",
                model if estimate_provider == "claude" else None,
                pricing,
                output_budget,
            ),
            _advice_card(
                conn,
                flt,
                report,
                settings.features.llm_analysis,
                "codex",
                model if estimate_provider == "codex" else None,
                pricing,
                output_budget,
            ),
        ]
        if report and report.total_sessions and settings.features.llm_analysis.enabled
        else []
    )
    context = {
        "filter": flt,
        "run": run,
        "report": report,
        "project_names": queries.project_names(conn),
        "detail_urls": (
            {
                item.session_id: f"/context-analysis?{_query(flt, session_id=item.session_id)}"
                for item in report.sessions
            }
            if report
            else {}
        ),
        "chart_data": embed_json(_chart_data(report.selected if report else None)),
        "advice_cards": advice_cards,
        "advice_query": _query(flt),
    }
    name = (
        "partials/_context_analysis.html"
        if request.headers.get("HX-Request")
        else "pages/context_analysis.html"
    )
    return templates.TemplateResponse(request, name, context)


@router.post("/context-analysis/advice/{provider}", response_class=HTMLResponse)
def run_context_advice(
    provider: Provider,
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    settings: SettingsDep,
    pricing: PricingDep,
    model: Annotated[str, Form()],
    force: Annotated[bool, Form()] = False,
    output_budget: Annotated[int, Form(ge=0, le=100000)] = 600,
    selected_ids: Annotated[list[str] | None, Query()] = None,
    scope: Annotated[Literal["filtered", "top", "selected"], Query()] = "filtered",
    agent: Annotated[Literal["", "claude", "codex"] | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    include_noise: Annotated[bool, Query()] = False,
    min_calls: Annotated[int, Query(ge=1, le=100_000)] = 1,
    min_turns: Annotated[int, Query(ge=0, le=100_000)] = 0,
    top_n: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[SortKey, Query()] = "input_tokens",
) -> HTMLResponse:
    flt = analysis.ContextAnalysisFilter(
        agent=agent or None,
        project=project or None,
        since=since or None,
        until=until or None,
        include_noise=include_noise,
        min_calls=min_calls,
        min_turns=min_turns,
        top_n=top_n,
        sort=sort,
        selected_ids=tuple(selected_ids or []),
        scope=scope,
    )
    report = analysis.build_context_report(conn, flt, pricing)
    config = settings.features.llm_analysis
    card = _advice_card(conn, flt, report, config, provider, model, pricing, output_budget)
    card["advice_query"] = _query(flt)
    if not config.enabled:
        card["error"] = "LLM analysis is disabled in the config."
    elif report.total_sessions == 0:
        card["error"] = "No context analysis data matches these filters."
    if "error" not in card:
        backend = (
            ClaudeCliBackend()
            if provider == "claude"
            else CodexCliBackend(executable=config.codex_executable)
        )
        try:
            card["advice"] = context_advisor.run(
                conn,
                backend,
                flt,
                report,
                backend_name=provider,
                model=model,
                force=force,
            )
        except LLMError as exc:
            card["error"] = (
                str(exc)
                if provider == "codex"
                else "Claude could not produce context advice. Check CLI sign-in and retry."
            )
    return templates.TemplateResponse(request, "partials/_context_advice.html", card)


@router.get("/context-analysis/scenarios", response_class=HTMLResponse)
def scenario_results(
    request: Request,
    templates: TemplatesDep,
    pricing: PricingDep,
    scenario: Annotated[Scenario, Query()],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/_scenario_results.html",
        {
            "rows": compare_scenarios(scenario, scenario.model, pricing),
            "low": compare_scenarios(
                scenario.model_copy(update={"cache_hit": max(0, scenario.cache_hit - 0.1)}),
                scenario.model,
                pricing,
            ),
            "high": compare_scenarios(
                scenario.model_copy(update={"cache_hit": min(1, scenario.cache_hit + 0.1)}),
                scenario.model,
                pricing,
            ),
        },
    )


@router.get("/context-analysis/profiles", response_class=HTMLResponse)
def profiles_page(request: Request, templates: TemplatesDep) -> HTMLResponse:
    import shutil

    from wdi.context_profiles import OUTPUT_POLICY, PRESERVE, detect_codex_keys

    keys = detect_codex_keys()
    return templates.TemplateResponse(
        request,
        "pages/context_profiles.html",
        {
            "claude_installed": shutil.which("claude") is not None,
            "codex_supported": {"compact_prompt", "model_auto_compact_token_limit"} <= keys,
            "preserve": PRESERVE,
            "output_policy": OUTPUT_POLICY,
        },
    )
