"""Independent Claude and Codex coaching cards."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from wdi.config import LlmAnalysisConfig
from wdi.llm import coach
from wdi.llm.backend import ClaudeCliBackend, CodexCliBackend, LLMError
from wdi.metrics.compute import MetricsReport, build_report
from wdi.metrics.filters import SessionFilter
from wdi.web.dependencies import DbDep, PricingDep, SettingsDep, TemplatesDep
from wdi.web.filters import FilterDep, filter_query

router = APIRouter()
Provider = Literal["claude", "codex"]


def coach_card(
    conn: sqlite3.Connection,
    flt: SessionFilter,
    report: MetricsReport,
    config: LlmAnalysisConfig,
    provider: Provider,
    model: str | None = None,
) -> dict[str, object]:
    options = list(
        config.coach_models.claude if provider == "claude" else config.coach_models.codex
    )
    if provider == "claude":
        options = list(dict.fromkeys([config.model_for("coach"), *options]))
    selected = model if model is not None and model in options else options[0]
    card: dict[str, object] = {
        "provider": provider,
        "provider_label": "Claude" if provider == "claude" else "Codex",
        "models": options,
        "selected_model": selected,
        "coach_query": filter_query(flt),
        "coach": coach.get_cached_coach(conn, flt, report, model=selected, backend_name=provider),
    }
    if model is not None and model not in options:
        card["error"] = "Choose a model from the configured list."
    return card


@router.get("/coach/{provider}", response_class=HTMLResponse)
def select_coach_model(
    provider: Provider,
    request: Request,
    conn: DbDep,
    pricing: PricingDep,
    templates: TemplatesDep,
    settings: SettingsDep,
    flt: FilterDep,
    model: str | None = None,
) -> HTMLResponse:
    card = coach_card(
        conn, flt, build_report(conn, flt, pricing), settings.features.llm_analysis, provider, model
    )
    if not settings.features.llm_analysis.enabled:
        card["error"] = "LLM analysis is disabled in the config."
    return templates.TemplateResponse(request, "partials/_coach.html", card)


@router.post("/coach/{provider}", response_class=HTMLResponse)
def run_coach(
    provider: Provider,
    request: Request,
    conn: DbDep,
    pricing: PricingDep,
    templates: TemplatesDep,
    settings: SettingsDep,
    flt: FilterDep,
    model: Annotated[str, Form()],
    force: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    config = settings.features.llm_analysis
    report = build_report(conn, flt, pricing)
    card = coach_card(conn, flt, report, config, provider, model)
    if not config.enabled:
        card["error"] = "LLM analysis is disabled in the config."
    if "error" not in card:
        backend = (
            ClaudeCliBackend()
            if provider == "claude"
            else CodexCliBackend(executable=config.codex_executable)
        )
        try:
            card["coach"] = coach.run_coach(
                conn, backend, flt, report, model=model, backend_name=provider, force=force
            )
        except LLMError as exc:
            card["error"] = (
                str(exc)
                if provider == "codex"
                else (
                    f"{card['provider_label']} could not produce advice. "
                    "Check CLI installation, sign-in and model access, then retry."
                )
            )
    return templates.TemplateResponse(request, "partials/_coach.html", card)
