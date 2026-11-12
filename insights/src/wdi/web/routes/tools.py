"""Tool drill-down routes: global per-tool stats and per-tool session lists."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from wdi.metrics import queries
from wdi.metrics.compute import ToolDetailStat
from wdi.web.dependencies import DbDep, TemplatesDep
from wdi.web.filters import FilterDep

router = APIRouter()


@router.get("/tools", response_class=HTMLResponse)
def tool_list(
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    flt: FilterDep,
) -> HTMLResponse:
    details = [
        ToolDetailStat(
            tool_name=r["tool_name"],
            calls=r["calls"],
            errors=r["errors"],
            avg_duration_ms=r["avg_duration_ms"],
            output_bytes=r["output_bytes"],
        )
        for r in queries.tool_detail(conn, flt, limit=50)
    ]
    context = {
        "tools": details,
        "filter": flt,
        "filter_action": "/tools",
        "filter_target": "#tool-table",
        "project_names": queries.project_names(conn),
    }
    name = "partials/_tool_table.html" if request.headers.get("HX-Request") else "pages/tools.html"
    return templates.TemplateResponse(request, name, context)


@router.get("/tools/{tool_name}", response_class=HTMLResponse)
def tool_sessions(
    tool_name: str,
    request: Request,
    conn: DbDep,
    templates: TemplatesDep,
    flt: FilterDep,
) -> HTMLResponse:
    rows = queries.tool_sessions(conn, flt, tool_name)
    context = {"tool_name": tool_name, "rows": rows, "filter": flt}
    return templates.TemplateResponse(request, "pages/tool_detail.html", context)
