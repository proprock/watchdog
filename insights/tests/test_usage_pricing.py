"""Raw-fact and CLI pricing parity after WD-198."""

import json
import sys
from decimal import Decimal
from uuid import uuid4

import pytest
from fact_fixtures import TARIFFS, build_v5, claude_usage, cli, codex_hook, codex_usage

from agent_watchdog.config import Config, Project, save_config
from agent_watchdog.storage import Store


@pytest.fixture
def project():
    return uuid4()


@pytest.fixture
def tariffs_file(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text(TARIFFS, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def migrated_queries():
    global facts_query, pricing
    from wdi.metrics import pricing
    from wdi.metrics import usage as facts_query


def _seed(root, project, *, twin=False):
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
    if twin:
        # WD-128: the same Codex response stored again under another event id.
        events.append(events[1].model_copy(update={"event_id": uuid4()}))
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


def test_cost_prices_a_response_stored_twice_once(tmp_path, project, tariffs_file):
    _seed(tmp_path, project, twin=True)
    tariffs = pricing.Tariffs.load(tariffs_file)
    with Store(tmp_path, project) as store:
        rows = {
            row["provider"]: row
            for row in facts_query.cost(store.connection, tariffs, group_by="provider")
        }

    assert rows["codex"]["cost_estimate"] == "3.250000"


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


def test_usage_cli_explicit_tariffs_preserve_raw_counters_and_label_estimates(
    tmp_path, project, monkeypatch, capsys, tariffs_file
):
    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    _seed(tmp_path / "data" / "projects" / str(project), project)
    base = ["usage", "--project", str(project)]

    monkeypatch.setattr(sys, "argv", base)
    code, result = cli(tmp_path, *sys.argv)
    assert code == 0
    print(json.dumps(result))
    plain = json.loads(capsys.readouterr().out)
    assert plain["pricing"]["estimate"] is True

    monkeypatch.setattr(sys, "argv", [*base, "--tariffs", str(tariffs_file)])
    code, result = cli(tmp_path, *sys.argv)
    assert code == 0
    print(json.dumps(result))
    priced = json.loads(capsys.readouterr().out)
    assert priced["token_usage"] == plain["token_usage"]  # raw sums untouched
    assert priced["pricing"]["estimate"] is True
    assert priced["pricing"]["currency"] == "USD"
    assert "not billed spend" in priced["pricing"]["caveat"]
    totals = {r["provider"]: r for r in priced["pricing"]["by"]["provider"]}
    assert totals["claude"]["cost_estimate"] == "8.400000"


@pytest.mark.parametrize("command", ["usage", "overview"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("cached", [0, 600_000, 1_200_000, None])
def test_tariff_commands_price_provider_input_conventions(
    tmp_path, project, monkeypatch, capsys, tariffs_file, command, provider, cached
):
    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    counts = {"input_tokens": 1_000_000, "output_tokens": 200_000}
    if cached is not None:
        counts["cached_input_tokens" if provider == "codex" else "cache_read_input_tokens"] = cached
    if provider == "codex":
        events = [
            codex_hook(
                project,
                session=session,
                kind="tool.finish",
                offset=1,
                turn_id="t1",
                model="gpt-5.6",
            ),
            codex_usage(
                project, session=session, offset=2, response_id="r1", turn_id="t1", delta=counts
            ),
        ]
    else:
        # Use the same model name: the provider, not the tariff/model, defines the convention.
        events = [
            claude_usage(
                project,
                session=session,
                offset=2,
                request_id="r1",
                model="gpt-5.6",
                response=counts,
            )
        ]
    root = tmp_path / "data" / "projects" / str(project)
    root.mkdir(parents=True)
    build_v5(root, project, events)
    with Store(root, project):
        pass
    base = [command]
    if command == "usage":
        base += ["--project", str(project)]
    monkeypatch.setattr(sys, "argv", [*base, "--tariffs", str(tariffs_file)])
    code, result = cli(tmp_path, *sys.argv)
    assert code == 0
    print(json.dumps(result))
    result = json.loads(capsys.readouterr().out)
    usage = result if command == "usage" else result["projects"][0]["usage"]
    raw = usage["token_usage"][0]
    assert raw["input_tokens"] == 1_000_000
    assert raw["cached_input_tokens"] == cached
    uncached = (
        1_000_000
        if provider == "claude"
        else (max(0, 1_000_000 - cached) if cached is not None else None)
    )
    expected = Decimal("2") + Decimal(uncached or 0) * Decimal("1.25") / 1_000_000
    expected += Decimal(cached or 0) * Decimal("0.125") / 1_000_000
    groups = (
        usage["pricing"]["by"].values() if command == "usage" else [usage["pricing"]["by_provider"]]
    )
    for rows in groups:
        assert len(rows) == 1
        row = rows[0]
        assert row["cost_estimate"] == str(expected.quantize(pricing.QUANTUM))
        assert row["by_field"]["input"]["tokens"] == (uncached or 0)
        assert row["by_field"]["cache_read"]["tokens"] == (cached or 0)
        unavailable = provider == "codex" and cached is None
        assert row["unpriced_tokens"] == (1_000_000 if unavailable else 0)
        assert row["unpriced_reasons"] == ({"cached_input_unavailable": 1} if unavailable else {})
