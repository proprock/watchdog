"""FastAPI application factory for the local analytics server."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wdi.metrics.pricing import PricingResolver
from wdi.report.charts import register_jinja_filters
from wdi.web.routes import coach, context_analysis, dashboard, sessions, tools

if TYPE_CHECKING:
    from wdi.config import Settings

_WEB_DIR = Path(__file__).resolve().parent
_REPORT_STATIC = Path(str(resources.files("wdi.report") / "static"))


def create_app(settings: Settings) -> FastAPI:
    """Build the app, wiring immutable settings/pricing/templates onto app.state."""
    app = FastAPI(title="watchdog-insights", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
    register_jinja_filters(templates.env)

    app.state.settings = settings
    app.state.pricing = PricingResolver(settings.pricing)
    app.state.templates = templates

    # Vendored Chart.js is reused from the report package; htmx/CSS/JS are local.
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
    app.mount("/vendor", StaticFiles(directory=str(_REPORT_STATIC)), name="vendor")

    app.include_router(dashboard.router)
    app.include_router(sessions.router)
    app.include_router(tools.router)
    app.include_router(coach.router)
    app.include_router(context_analysis.router)

    @app.exception_handler(500)
    async def _on_error(request: Request, exc: Exception) -> HTMLResponse:  # noqa: RUF029
        # Never leak tracebacks, SQL or paths to the browser.
        return templates.TemplateResponse(
            request, "pages/error.html", {"status": 500}, status_code=500
        )

    return app
