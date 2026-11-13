"""Dated tariffs must be resolved before model totals are combined."""

from test_metrics import add_session

from wdi.db.connection import connect
from wdi.metrics.compute import build_report
from wdi.metrics.filters import SessionFilter


def test_stats_prices_each_session_at_its_historical_date(tmp_path):
    from wdi.metrics.pricing import PricingResolver, Tariffs

    path = tmp_path / "dated.toml"
    path.write_text(
        '["2026-01-01"]\nmodel = {input="1",output="0",cache_read="0",cache_write="0"}\n'
        '["2026-07-01"]\nmodel = {input="2",output="0",cache_read="0",cache_write="0"}\n',
        encoding="utf-8",
    )
    with connect(tmp_path / "metrics.sqlite") as conn:
        for session, started in (
            ("earlier", "2026-06-01T00:00:00Z"),
            ("later", "2026-08-01T00:00:00Z"),
        ):
            add_session(
                conn,
                session,
                model="model-v2",
                started=started,
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
            )
        report = build_report(conn, SessionFilter(), PricingResolver(Tariffs.load(path)))
    assert report.overview.estimated_cost == 3.0
    assert report.models[0].cost == 3.0
