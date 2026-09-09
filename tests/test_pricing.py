"""List-price cost estimates over v6 token telemetry (WD-114).

Estimates are recomputed from raw counters each call; nothing is persisted and
no counter is ever a synthetic total. Reasoning/thinking tokens and Codex
`total_tokens` are never priced (subset / overlapping sum).
"""

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from test_storage_v6 import build_v5, claude_usage, codex_hook, codex_usage

from agent_watchdog import facts_query, pricing
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, save_config
from agent_watchdog.storage import Store

TARIFFS = """\
currency = "USD"

["2026-01-01"]
"claude-sonnet-5" = { input = "3.00", output = "15.00", cache_read = "0.30", cache_write = "3.75" }
"gpt-5.6" = { input = "1.25", output = "10.00", cache_read = "0.125", cache_write = "0" }

["2026-09-09"]
"claude-sonnet-5" = { input = "2.00", output = "10.00", cache_read = "0.20", cache_write = "2.50" }
"gpt-5.6" = { input = "1.25", output = "10.00", cache_read = "0.125", cache_write = "0" }
"""


@pytest.fixture
def project():
    return uuid4()


@pytest.fixture
def tariffs_file(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text(TARIFFS, encoding="utf-8")
    return path


def test_load_rejects_a_malformed_or_missing_file(tmp_path):
    with pytest.raises(pricing.PricingError):
        pricing.Tariffs.load(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text('["2026-01-01"]\n"m" = { input = "-1" }\n', encoding="utf-8")
    with pytest.raises(pricing.PricingError):
        pricing.Tariffs.load(bad)
    notadate = tmp_path / "nd.toml"
    notadate.write_text('["v1"]\n"m" = { input = "1" }\n', encoding="utf-8")
    with pytest.raises(pricing.PricingError):
        pricing.Tariffs.load(notadate)


def test_rate_picks_the_latest_effective_section_at_or_before_the_timestamp(tariffs_file):
    tariffs = pricing.Tariffs.load(tariffs_file)
    assert tariffs.currency == "USD"

    early = tariffs.rate("claude-sonnet-5", datetime(2026, 6, 1, tzinfo=UTC))
    assert early is not None and early.effective.isoformat() == "2026-01-01"
    assert early.rates["output"] == Decimal("15.00")

    late = tariffs.rate("claude-sonnet-5", datetime(2026, 9, 10, tzinfo=UTC))
    assert late is not None and late.effective.isoformat() == "2026-09-09"
    assert late.rates["output"] == Decimal("10.00")

    assert tariffs.rate("claude-sonnet-5", datetime(2025, 1, 1, tzinfo=UTC)) is None  # too early
    absent = tariffs.rate("claude-opus-5", datetime(2026, 6, 1, tzinfo=UTC))
    assert absent is not None and absent.rates == {}  # section applies, model missing


def test_estimate_prices_only_the_four_billable_classes(tariffs_file):
    tariffs = pricing.Tariffs.load(tariffs_file)
    rate = tariffs.rate("claude-sonnet-5", datetime(2026, 6, 1, tzinfo=UTC))
    assert rate is not None
    counts = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 2_000_000,
        "cache_write_input_tokens": 400_000,
        "output_tokens": 500_000,
        "reasoning_output_tokens": 300_000,  # subset of output -> never priced
        "total_tokens": 9_999_999,  # overlapping sum -> never priced
    }
    cost, provenance = pricing.estimate(counts, rate)
    # 1*3.00 + 2*0.30 + 0.4*3.75 + 0.5*15.00 = 3.00 + 0.60 + 1.50 + 7.50 = 12.60
    assert cost == Decimal("12.600000")
    assert provenance["unpriced_tokens"] == 0
    assert provenance["priced_fields"] == ["input", "output", "cache_read", "cache_write"]


def test_estimate_marks_a_missing_rate_field_unpriced_without_inventing_a_value(tmp_path):
    partial = tmp_path / "partial.toml"
    partial.write_text(
        '["2026-01-01"]\n"m" = { input = "1.00", output = "2.00" }\n', encoding="utf-8"
    )
    tariffs = pricing.Tariffs.load(partial)
    rate = tariffs.rate("m", datetime(2026, 2, 1, tzinfo=UTC))
    assert rate is not None
    cost, provenance = pricing.estimate(
        {"input_tokens": 1_000_000, "cached_input_tokens": 5_000_000}, rate
    )
    assert cost == Decimal("1.000000")
    assert provenance["unpriced_tokens"] == 5_000_000
    assert provenance["unpriced_reason"] == "rate_field_missing"


def _seed(root, project):
    root.mkdir(parents=True, exist_ok=True)
    session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    events = [
        codex_hook(
            project, session=session, kind="tool.finish", offset=1, turn_id="t1", model="gpt-5.6"
        ),
        codex_usage(
            project,
            session=session,
            offset=2,
            response_id="r1",
            turn_id="t1",
            delta={
                "input_tokens": 1_000_000,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 200_000,
                "reasoning_output_tokens": 50_000,
                "total_tokens": 1_200_000,
            },
        ),
        claude_usage(
            project,
            session=session,
            offset=3,
            request_id="c1",
            model="claude-sonnet-5",
            response={
                "input_tokens": 1_000_000,
                "cache_read_input_tokens": 2_000_000,
                "cache_creation_input_tokens": 400_000,
                "output_tokens": 500_000,
                "thinking_tokens": 300_000,
            },
        ),
    ]
    build_v5(root, project, events)
    with Store(root, project):
        pass
    return events


def test_cost_prices_each_usage_row_then_groups(tmp_path, project, tariffs_file):
    _seed(tmp_path, project)
    tariffs = pricing.Tariffs.load(tariffs_file)
    with Store(tmp_path, project) as store:
        by_provider = {
            row["provider"]: row
            for row in facts_query.cost(store.connection, tariffs, group_by="provider")
        }

    codex = by_provider["gpt-5.6"] if "gpt-5.6" in by_provider else by_provider["codex"]
    # gpt-5.6 @ 2026-09-09 section (usage row is dated BASE = 2026-09-09):
    # 1*1.25 + 0.2*10.00 = 1.25 + 2.00 = 3.25
    assert codex["cost_estimate"] == "3.250000"
    assert codex["unpriced_tokens"] == 0

    claude = by_provider["claude"]
    # claude-sonnet-5 @ 2026-09-09 section: 1*2.00 + 2*0.20 + 0.4*2.50 + 0.5*10.00
    #  = 2.00 + 0.40 + 1.00 + 5.00 = 8.40
    assert claude["cost_estimate"] == "8.400000"
    assert claude["currency"] == "USD"
    assert claude["by_field"]["cache_write"]["tokens"] == 400_000


def test_cost_flags_unpriced_models_and_pre_tariff_usage(tmp_path, project):
    root = tmp_path / "data" / "x"
    _seed(root, project)
    only_claude = tmp_path / "t.toml"
    only_claude.write_text(
        '["2026-09-09"]\n'
        '"claude-sonnet-5" = '
        '{ input = "2", output = "10", cache_read = "0.2", cache_write = "2.5" }\n',
        encoding="utf-8",
    )
    tariffs = pricing.Tariffs.load(only_claude)
    with Store(root, project) as store:
        rows = {
            r["provider"]: r
            for r in facts_query.cost(store.connection, tariffs, group_by="provider")
        }
    assert rows["codex"]["cost_estimate"] == "0.000000"
    assert rows["codex"]["unpriced_tokens"] > 0
    assert "model_not_in_tariff" in rows["codex"]["unpriced_reasons"]


def test_usage_cli_price_flag_adds_a_pricing_block_and_no_flag_changes_nothing(
    tmp_path, project, monkeypatch, capsys, tariffs_file
):
    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    _seed(tmp_path / "data" / "projects" / str(project), project)
    base = ["agent-watchdog", "--home", str(tmp_path), "usage", "--project", str(project)]

    monkeypatch.setattr(sys, "argv", base)
    assert main() == 0
    plain = json.loads(capsys.readouterr().out)
    assert "pricing" not in plain

    monkeypatch.setattr(sys, "argv", [*base, "--tariffs", str(tariffs_file)])
    assert main() == 0
    priced = json.loads(capsys.readouterr().out)
    assert priced["token_usage"] == plain["token_usage"]  # raw sums untouched
    assert priced["pricing"]["estimate"] is True
    assert priced["pricing"]["currency"] == "USD"
    assert "not billed spend" in priced["pricing"]["caveat"]
    totals = {r["provider"]: r for r in priced["pricing"]["by"]["provider"]}
    assert totals["claude"]["cost_estimate"] == "8.400000"
