"""Dashboard routes: live overview, charts and tables with HTMX filters."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from wdi.metrics import queries
from wdi.metrics.compute import build_report
from wdi.report.charts import chart_data, embed_json
from wdi.web.dependencies import DbDep, PricingDep, SettingsDep, TemplatesDep
from wdi.web.filters import FilterDep, filter_query
from wdi.web.routes.coach import coach_card

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    conn: DbDep,
    pricing: PricingDep,
    templates: TemplatesDep,
    settings: SettingsDep,
    flt: FilterDep,
) -> HTMLResponse:
    report = build_report(conn, flt, pricing)
    context = {
        "report": report,
        "overview": report.overview,
        "chart_data": embed_json(chart_data(report)),
        "filter": flt,
        "filter_action": "/",
        "filter_target": "#dashboard",
        "project_names": queries.project_names(conn),
        "coach_enabled": settings.features.llm_analysis.enabled,
        "coach_cards": [
            coach_card(conn, flt, report, settings.features.llm_analysis, "claude"),
            coach_card(conn, flt, report, settings.features.llm_analysis, "codex"),
        ],
        "coach_query": filter_query(flt),
    }
    # HTMX swaps only the dashboard fragment; normal navigation gets the full page.
    name = (
        "partials/_dashboard.html" if request.headers.get("HX-Request") else "pages/dashboard.html"
    )
    return templates.TemplateResponse(request, name, context)
