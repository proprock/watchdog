"""Read-only v6 telemetry comparison surface (WD-112).

Aggregates come only from ``event_facts`` raw columns: no synthetic totals, no
``json_extract`` in normal analysis, and the four documented query shapes hit
their partial indexes.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from fact_fixtures import BASE, build_v5, sample_events, seed

from agent_watchdog.storage import Store


@pytest.fixture
def project():
    return uuid4()


@pytest.fixture(autouse=True)
def migrated_queries():
    global facts_query
    from wdi.metrics import usage as facts_query


def test_token_usage_sums_raw_counters_by_provider_without_a_synthetic_total(tmp_path, project):
    seed(tmp_path, project)
    with Store(tmp_path, project) as store:
        rows = {
            r["provider"]: r for r in facts_query.token_usage(store.connection, group_by="provider")
        }

    assert rows["codex"]["input_tokens"] == 100
    assert rows["codex"]["reasoning_output_tokens"] == 5
    assert rows["codex"]["total_tokens"] == 120
    assert rows["codex"]["usage_rows"] == 1

    assert rows["claude"]["input_tokens"] == 3
    assert rows["claude"]["cached_input_tokens"] == 5000
    assert rows["claude"]["output_tokens"] == 400
    # Anthropic reports no total; it is never synthesised from the parts.
    assert rows["claude"]["total_tokens"] is None
    assert rows["claude"]["total_tokens_rows"] == 0


def test_token_usage_groups_by_model_and_effort(tmp_path, project):
    seed(tmp_path, project)
    with Store(tmp_path, project) as store:
        by_model = {
            r["model"]: r for r in facts_query.token_usage(store.connection, group_by="model")
        }
        by_effort = {
            r["reasoning_effort"]: r
            for r in facts_query.token_usage(store.connection, group_by="effort")
        }
    assert by_model["gpt-5.6"]["output_tokens"] == 20
    assert by_model["claude-sonnet-5"]["output_tokens"] == 400
    assert by_effort["high"]["output_tokens"] == 400
    assert by_effort[None]["output_tokens"] == 20  # codex usage inherited no effort


def test_token_usage_time_window_filters_and_buckets(tmp_path, project):
    seed(tmp_path, project)
    cut = int((BASE + timedelta(seconds=8)).timestamp() * 1_000_000)
    with Store(tmp_path, project) as store:
        after = facts_query.token_usage(store.connection, since_us=cut, group_by="provider")
        hourly = facts_query.token_usage(store.connection, group_by="hour")
    assert {r["provider"] for r in after} == {"claude"}
    assert sum(r["output_tokens"] for r in hourly) == 420


def test_a_response_stored_twice_counts_once_in_its_first_window(tmp_path, project):
    # WD-128: before the reader keyed Codex usage by response, two spellings of
    # one rollout path stored some responses under two event ids. Stored rows
    # stay authoritative; reads count each response once.
    events = sample_events(project)
    first = next(event for event in events if event.kind == "usage" and event.provider == "codex")
    twin = first.model_copy(
        update={"event_id": uuid4(), "received_at": BASE + timedelta(seconds=20)}
    )
    build_v5(tmp_path, project, [*events, twin])
    cut = int((BASE + timedelta(seconds=20)).timestamp() * 1_000_000)
    with Store(tmp_path, project) as store:
        total = {r["provider"]: r for r in facts_query.token_usage(store.connection)}
        after = facts_query.token_usage(store.connection, since_us=cut)

    assert (total["codex"]["usage_rows"], total["codex"]["input_tokens"]) == (1, 100)
    assert after == []


def test_process_efficiency_reports_tool_cost_error_rate_and_throughput(tmp_path, project):
    seed(tmp_path, project)
    with Store(tmp_path, project) as store:
        process = facts_query.process_efficiency(store.connection)

    bash = next(t for t in process["tool_cost"] if t["tool_name"] == "Bash")
    assert bash["finishes"] == 2
    assert bash["errors"] == 1
    assert bash["error_rate"] == 0.5
    assert bash["duration_ms"]["max"] == 250
    assert process["permission_throughput"][0]["permission_mode"] == "auto"


def test_coverage_is_reported_separately_from_aggregates(tmp_path, project):
    seed(tmp_path, project)
    with Store(tmp_path, project) as store:
        cover = facts_query.coverage(store.connection)
    assert cover["events"] == 7
    assert cover["model"]["observed"] == 5  # 3 codex hooks + codex-usage inherit + claude usage
    assert cover["model"]["attribution"]["inherited"] == 1
    assert cover["reasoning_effort"]["attribution"]["unavailable"] >= 1


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        (
            "SELECT SUM(output_tokens) FROM event_facts "
            "WHERE kind='usage' AND received_at_us>=0 AND received_at_us<9",
            "event_facts_usage_time",
        ),
        (
            "SELECT SUM(output_tokens) FROM event_facts "
            "WHERE kind='usage' AND provider='claude' AND model='claude-sonnet-5'",
            "event_facts_usage_dim",
        ),
        (
            "SELECT tool_name, COUNT(*) FROM event_facts "
            "WHERE kind='tool.finish' AND provider='claude' GROUP BY tool_name",
            "event_facts_tool_cost",
        ),
        (
            "SELECT tool_use_id FROM event_facts "
            "WHERE kind IN ('tool.start','tool.finish') AND provider='claude' "
            "AND session_id='s' AND turn_id='t'",
            "event_facts_tool_pair",
        ),
    ],
)
def test_documented_query_shapes_use_their_partial_index(tmp_path, project, sql, index):
    build_v5(tmp_path, project, [])
    with Store(tmp_path, project) as store:
        plan = " ".join(
            str(part)
            for row in store.connection.execute("EXPLAIN QUERY PLAN " + sql)
            for part in row
        )
    assert index in plan, plan
    assert "json_extract" not in plan


def test_usage_cli_is_read_only_and_filters_since(tmp_path, project):
    from fact_fixtures import cli

    from agent_watchdog.config import Config, Project, save_config

    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    root = tmp_path / "data" / "projects" / str(project)
    seed(root, project)
    before = (root / "events.sqlite3").read_bytes()
    code, result = cli(tmp_path, "usage", "--project", str(project))
    assert code == 0
    assert result["project"] == tmp_path.name
    assert {r["provider"]: r for r in result["token_usage"]}["codex"]["input_tokens"] == 100
    assert "coverage" in result and "process" in result
    code, result = cli(
        tmp_path,
        "usage",
        "--project",
        str(project),
        "--since",
        (BASE + timedelta(seconds=8)).isoformat(),
    )
    assert code == 0
    assert {r["provider"] for r in result["token_usage"]} == {"claude"}
    assert (root / "events.sqlite3").read_bytes() == before


def test_usage_cli_rejects_a_pre_v6_database(tmp_path, project):
    from fact_fixtures import cli

    from agent_watchdog.config import Config, Project, save_config

    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    root = tmp_path / "data" / "projects" / str(project)
    root.mkdir(parents=True)
    build_v5(root, project, [])
    code, result = cli(tmp_path, "usage", "--project", str(project))
    assert code == 1
    assert "v6" in result["message"]
