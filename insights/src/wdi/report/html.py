"""Render a self-contained HTML report from a MetricsReport.

Everything (Chart.js, CSS, JS, data) is inlined into one file so the report
works offline and is trivial to share. Chart data is passed as JSON in a
<script type="application/json"> island the client script reads on load.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from importlib import resources

from jinja2 import Environment, PackageLoader, select_autoescape

from wdi.metrics.compute import MetricsReport
from wdi.report.charts import chart_data, embed_json, register_jinja_filters

_env = Environment(
    loader=PackageLoader("wdi.report", "templates"),
    autoescape=select_autoescape(["html"]),
)
register_jinja_filters(_env)


def _asset(name: str) -> str:
    return (resources.files("wdi.report.static") / name).read_text(encoding="utf-8")


def render_report(
    report: MetricsReport,
    *,
    title: str = "watchdog-insights report",
    generated_at: datetime | None = None,
) -> str:
    template = _env.get_template("report.html")
    return template.render(
        title=title,
        generated_at=(generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC"),
        report=report,
        overview=report.overview,
        chart_data=embed_json(chart_data(report)),
        chart_js=_asset("chart.umd.min.js"),
        report_css=_asset("report.css"),
        report_js=_asset("report.js"),
    )


def report_payload(report: MetricsReport) -> dict[str, object]:
    """Expose the structured report (used by tests and potential JSON export)."""
    return {
        "overview": asdict(report.overview),
        "models": [asdict(m) for m in report.models],
        "unpriced_models": report.unpriced_models,
        "activity": [asdict(b) for b in report.activity],
        "tool_categories": [asdict(c) for c in report.tool_categories],
        "failing_tools": [asdict(f) for f in report.failing_tools],
        "projects": [asdict(p) for p in report.projects],
        "subagents": [asdict(s) for s in report.subagents],
    }
