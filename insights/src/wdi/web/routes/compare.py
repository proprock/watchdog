"""Compare two cohorts of sessions."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from wdi.metrics.compare import compare, parse_filter
from wdi.web.dependencies import DbDep, PricingDep, SettingsDep, TemplatesDep

router = APIRouter()


@router.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    conn: DbDep,
    pricing: PricingDep,
    settings: SettingsDep,
    templates: TemplatesDep,
    a: str = "",
    b: str = "",
) -> HTMLResponse:
    result = None
    error = None
    if a or b:
        try:
            if not a or not b:
                raise ValueError("Enter a JSON filter for each cohort.")
            filter_a = parse_filter(a)
            filter_b = parse_filter(b)
            result = compare(conn, pricing, filter_a, filter_b, settings)
        except ValueError as exc:
            error = f"Invalid filter: {exc}"
    return templates.TemplateResponse(
        request,
        "pages/compare.html",
        {"a": a, "b": b, "result": result, "error": error},
        status_code=400 if error else 200,
    )
