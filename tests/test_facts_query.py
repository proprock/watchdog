"""Read-only v6 telemetry comparison surface (WD-112).

Aggregates come only from ``event_facts`` raw columns: no synthetic totals, no
``json_extract`` in normal analysis, and the four documented query shapes hit
their partial indexes.
"""

import json
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from test_storage_v6 import BASE, build_v5, claude_hook, claude_usage, codex_hook, codex_usage

from agent_watchdog import facts_query
from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, save_config
from agent_watchdog.storage import Store

SESSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def project():
    return uuid4()


def sample_events(project):
    return [
        codex_hook(
            project, session=SESSION_A, kind="turn.start", offset=0, turn_id="t1", model="gpt-5.6"
        ),
        codex_hook(
            project, session=SESSION_A, kind="tool.start", offset=1, turn_id="t1", model="gpt-5.6"
        ),
        codex_hook(
            project, session=SESSION_A, kind="tool.finish", offset=3, turn_id="t1", model="gpt-5.6"
        ),
        codex_usage(
            project,
            session=SESSION_A,
            offset=4,
            response_id="r-a1",
            turn_id="t1",
            delta={
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 0,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
            },
        ),
        claude_hook(
            project,
            session=SESSION_B,
            kind="tool.finish",
            offset=10,
            prompt_id="pp-1",
            effort="high",
            permission_mode="auto",
            tool_name="Bash",
            tool_use_id="u-1",
            duration_ms=250,
        ),
        claude_hook(
            project,
            session=SESSION_B,
            kind="tool.finish",
            offset=12,
            prompt_id="pp-1",
            effort="high",
            permission_mode="auto",
            tool_name="Bash",
            tool_use_id="u-2",
            duration_ms=90,
            hook_event_name="PostToolUseFailure",
        ),
        claude_usage(
            project,
            session=SESSION_B,
            offset=13,
            request_id="req-b1",
            model="claude-sonnet-5",
            response={
                "input_tokens": 3,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 100,
                "output_tokens": 400,
                "thinking_tokens": 50,
            },
        ),
    ]


def seed(root, project):
    root.mkdir(parents=True, exist_ok=True)
    build_v5(root, project, sample_events(project))
    with Store(root, project):
        pass  # reopen migrates v5 -> v6 and backfills event_facts


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


def _data_root(tmp_path, project):
    return tmp_path / "data" / "projects" / str(project)


def test_usage_cli_is_read_only_and_filters_since(tmp_path, project, monkeypatch, capsys):
    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    seed(_data_root(tmp_path, project), project)

    argv = ["agent-watchdog", "--home", str(tmp_path), "usage", "--project", str(project)]
    monkeypatch.setattr(sys, "argv", argv)
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project"] == tmp_path.name
    totals = {r["provider"]: r for r in result["token_usage"]}
    assert totals["codex"]["input_tokens"] == 100
    assert "coverage" in result and "process" in result

    cut = (BASE + timedelta(seconds=8)).isoformat()
    monkeypatch.setattr(sys, "argv", [*argv, "--since", cut])
    assert main() == 0
    filtered = json.loads(capsys.readouterr().out)
    assert {r["provider"] for r in filtered["token_usage"]} == {"claude"}


def test_usage_cli_rejects_a_pre_v6_database(tmp_path, project, monkeypatch, capsys):
    save_config(tmp_path / "config.toml", Config(projects=(Project(id=project, root=tmp_path),)))
    root = _data_root(tmp_path, project)
    root.mkdir(parents=True)
    build_v5(root, project, [])
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-watchdog", "--home", str(tmp_path), "usage", "--project", str(project)],
    )
    assert main() == 1
    assert "v6" in json.loads(capsys.readouterr().out)["message"]
