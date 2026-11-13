"""Offline contracts for the read-only cross-project `overview` command (WD-016)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from fact_fixtures import BASE, TARIFFS, _finish, build_v5, cli, registered, sample_events, seed

from agent_watchdog import _proc, analysis, inspection
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store


def register(tmp_path, *names, seeded=()):
    """Register one project per name; only the names in ``seeded`` get a v6 database."""
    projects = {name: Project(id=uuid4(), root=tmp_path / name) for name in names}
    save_config(tmp_path / "config.toml", Config(projects=tuple(projects.values())))
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    for name in seeded:
        seed(paths.project_data(projects[name].id), projects[name].id)
    return paths, projects


@pytest.fixture(autouse=True)
def migrated_queries():
    global facts_query
    from wdi.metrics import usage as facts_query


def overview(monkeypatch, capsys, tmp_path, *args):
    return cli(tmp_path, "overview", *args)


def rows(result):
    return {row["project"]: row for row in result["projects"]}


def refresh_all(store):
    """What the daemon does between ticks: materialize every session's findings."""
    for provider, session_id in store.pending_sessions():
        store.refresh_session_findings(provider, session_id)


def test_overview_reports_usage_findings_and_recency_per_project(tmp_path, monkeypatch, capsys):
    register(tmp_path, "a", "b", seeded=("a", "b"))

    code, result = overview(monkeypatch, capsys, tmp_path)

    assert code == 0 and result["ok"] and result["complete"]
    assert result["observed_event_count"] == 14
    for row in rows(result).values():
        usage = {item["provider"]: item for item in row["usage"]["token_usage"]}
        assert row["usage"]["state"] == "ready"
        assert usage["codex"]["input_tokens"] == 100
        assert usage["claude"]["output_tokens"] == 400
        assert row["event_count"] == 7 and row["session_count"] == 2
        assert row["last_activity"] is not None
        assert row["findings"]["state"] == "ready"
        assert set(row["findings"]["by_provider"]) == {"codex", "claude"}
        assert row["usage"]["pricing"]["estimate"] is True


def test_findings_count_a_fired_rule_per_provider(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "loopy")
    project = projects["loopy"]
    with Store(paths.project_data(project.id), project.id) as store:
        for at in range(3):
            store.put(_finish(project.id, at=at))
        refresh_all(store)

    _, result = overview(monkeypatch, capsys, tmp_path)

    by_provider = rows(result)["loopy"]["findings"]["by_provider"]
    assert by_provider["claude"]["by_rule"].get("repeated_tool_outcome", 0) >= 1, by_provider
    assert by_provider["codex"]["by_rule"] == {}


def _turn(project, checkout, session, kind, at):
    return Envelope(
        provider="codex",
        project_id=project.id,
        checkout_id=checkout,
        session_id=session,
        kind=kind,
        source="hook",
        received_at=at,
    )


def _noisy_events(project, checkout):
    """Both providers fire a repeat rule; the codex checkout also oscillates."""
    start = datetime(2026, 9, 21, tzinfo=UTC)
    events = [
        _finish(project.id, at=at, provider=provider, session_id=f"{provider}-s")
        for provider in ("claude", "codex")
        for at in range(3)
    ]
    events += [
        _turn(project, checkout, "codex-s", "turn.start", start),
        _turn(project, checkout, "codex-s", "turn.end", start + timedelta(seconds=10)),
    ]
    # WD-146: an oscillation needs an edit between each pair of snapshots.
    events += [
        Envelope(
            provider="codex",
            project_id=project.id,
            checkout_id=checkout,
            session_id="codex-s",
            kind="tool.finish",
            source="hook",
            received_at=start + timedelta(seconds=seconds),
            payload={"codex": {"tool_name": "apply_patch"}},
        )
        for seconds in (1.5, 2.5)
    ]
    return events


def _expected_from_session_reports(paths, project):
    """The per-session ``report`` results, merged the way ``overview`` merges them."""
    by_rule: dict[str, dict[str, set[str]]] = {"codex": {}, "claude": {}}
    gaps: dict[str, set[str]] = {"codex": set(), "claude": set()}
    listing = inspection.sessions(paths, project, limit=1000, offset=0)["sessions"]
    for session in listing:
        found = inspection.report(
            paths, project, session_id=session["session_id"], provider=session["provider"]
        )
        gaps[session["provider"]].update(found["gaps"])
        for finding in found["findings"]:
            rule = by_rule[session["provider"]].setdefault(finding["rule"], set())
            rule.add(finding["fingerprint"])
    return {
        provider: {
            "by_rule": {rule: len(prints) for rule, prints in sorted(by_rule[provider].items())},
            "gaps": sorted(gaps[provider]),
        }
        for provider in by_rule
    }


def test_overview_findings_equal_the_sum_of_the_per_session_reports(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "noisy")
    project, checkout = projects["noisy"], uuid4()
    start = datetime(2026, 9, 21, tzinfo=UTC)
    with Store(paths.project_data(project.id), project.id) as store:
        for event in _noisy_events(project, checkout):
            store.put(event)
        for step, fingerprint in enumerate("121"):
            store.record_diff_snapshot(
                checkout, fingerprint * 64, 10, observed_at=start + timedelta(seconds=step + 1)
            )
        refresh_all(store)

    _, result = overview(monkeypatch, capsys, tmp_path)

    findings = rows(result)["noisy"]["findings"]
    assert findings["by_provider"] == _expected_from_session_reports(paths, project)
    assert findings["pending_sessions"] == 0 and findings["as_of"] is not None
    by_rule = {name: item["by_rule"] for name, item in findings["by_provider"].items()}
    assert "repeated_tool_outcome" in by_rule["claude"], findings
    assert "repeated_tool_outcome" in by_rule["codex"], findings
    assert "diff_oscillation" in by_rule["codex"], findings


def test_v8_findings_wait_for_versioned_refresh_without_read_only_migration(
    tmp_path, monkeypatch, capsys
):
    paths, projects = register(tmp_path, "legacy")
    project = projects["legacy"]
    root = paths.project_data(project.id)
    with Store(root, project.id) as store:
        for at in range(3):
            store.put(_finish(project.id, at=at))
        refresh_all(store)
    with sqlite3.connect(root / "events.sqlite3") as db:
        db.execute("ALTER TABLE session_analysis DROP COLUMN rule_version")
        db.execute("PRAGMA user_version=8")
    _, result = overview(monkeypatch, capsys, tmp_path)
    findings = rows(result)["legacy"]["findings"]
    assert findings["pending_sessions"] == 1
    assert findings["as_of"] is None
    assert findings["by_provider"]["claude"]["by_rule"] == {}
    with sqlite3.connect(root / "events.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8


def test_rule_release_hides_stale_findings_until_daemon_refresh(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "versioned")
    project = projects["versioned"]
    with Store(paths.project_data(project.id), project.id) as store:
        for event in _noisy_events(project, uuid4()):
            store.put(event)
        refresh_all(store)

    monkeypatch.setattr(analysis, "RULE_VERSION", "wd-010.test-release")
    _, result = overview(monkeypatch, capsys, tmp_path)
    findings = rows(result)["versioned"]["findings"]
    assert findings["pending_sessions"] == 2
    assert findings["as_of"] is None
    assert all(not item["by_rule"] for item in findings["by_provider"].values())

    with Store(paths.project_data(project.id), project.id) as store:
        # One session can be current while the other still waits for its tick.
        store.refresh_session_findings("claude", "claude-s")
    _, result = overview(monkeypatch, capsys, tmp_path)
    findings = rows(result)["versioned"]["findings"]
    assert findings["pending_sessions"] == 1
    assert findings["by_provider"]["claude"]["by_rule"]
    assert findings["by_provider"]["codex"]["by_rule"] == {}

    with Store(paths.project_data(project.id), project.id) as store:
        refresh_all(store)
    _, result = overview(monkeypatch, capsys, tmp_path)
    findings = rows(result)["versioned"]["findings"]
    assert findings["pending_sessions"] == 0
    assert findings["by_provider"] == _expected_from_session_reports(paths, project)


def test_sessions_the_daemon_has_not_refreshed_are_pending_not_zero(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "fresh")
    project = projects["fresh"]
    with Store(paths.project_data(project.id), project.id) as store:
        for event in _noisy_events(project, uuid4()):
            store.put(event)

    _, result = overview(monkeypatch, capsys, tmp_path)

    findings = rows(result)["fresh"]["findings"]
    assert findings["pending_sessions"] == 2 and findings["as_of"] is None
    assert findings["by_provider"]["claude"]["by_rule"] == {}


def test_a_pre_v8_store_marks_findings_unsupported(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "old")
    project = projects["old"]
    root = paths.project_data(project.id)
    root.mkdir(parents=True)
    build_v5(root, project.id, _noisy_events(project, uuid4()))

    code, result = overview(monkeypatch, capsys, tmp_path)

    assert code == 0 and not result["complete"]
    expected = {"state": "unsupported", "reason": "schema_predates_v8"}
    assert rows(result)["old"]["findings"] == expected


def test_overview_reads_findings_without_parsing_any_event(tmp_path, monkeypatch):
    paths, projects = register(tmp_path, "noisy")
    project = projects["noisy"]
    with Store(paths.project_data(project.id), project.id) as store:
        for event in _noisy_events(project, uuid4()):
            store.put(event)
        refresh_all(store)
    parsed = 0
    real_parse = inspection.persisted_envelope

    def counting(document):
        nonlocal parsed
        parsed += 1
        return real_parse(document)

    monkeypatch.setattr(inspection, "persisted_envelope", counting)

    from wdi.metrics.overview import overview as query_overview

    query_overview([registered(paths, project)], since=None, until=None)

    assert parsed == 0


def test_a_project_without_a_database_is_unknown_not_zero(tmp_path, monkeypatch, capsys):
    register(tmp_path, "a", "b", seeded=("a",))

    code, result = overview(monkeypatch, capsys, tmp_path)

    assert code == 0 and result["ok"] and not result["complete"]
    assert result["observed_event_count"] == 7
    absent = rows(result)["b"]
    assert absent["database"] == {"state": "unavailable"}
    assert absent["usage"] is None and absent["findings"] is None
    assert absent["last_activity"] is None


def test_a_corrupt_database_fails_only_its_own_row(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "a", "b", seeded=("a", "b"))
    (paths.project_data(projects["b"].id) / "events.sqlite3").write_bytes(b"not a database")

    code, result = overview(monkeypatch, capsys, tmp_path)

    assert code == 1 and not result["ok"] and not result["complete"]
    assert rows(result)["b"]["database"] == {"state": "error", "error": "DatabaseError"}
    assert rows(result)["b"]["usage"] is None
    assert rows(result)["a"]["usage"]["state"] == "ready"


def test_a_pre_v6_database_marks_usage_unsupported_and_keeps_other_sections(
    tmp_path, monkeypatch, capsys
):
    paths, projects = register(tmp_path, "old", "new", seeded=("new",))
    root = paths.project_data(projects["old"].id)
    root.mkdir(parents=True)
    build_v5(root, projects["old"].id, [])

    code, result = overview(monkeypatch, capsys, tmp_path)

    assert code == 0 and result["ok"] and not result["complete"]
    old = rows(result)["old"]
    assert old["database"] == {"state": "ready"}
    assert old["usage"] == {"state": "unsupported", "reason": "schema_predates_v6"}
    assert old["findings"]["state"] == "unsupported"
    assert rows(result)["new"]["usage"]["state"] == "ready"


def test_since_bounds_usage(tmp_path, monkeypatch, capsys):
    register(tmp_path, "a", seeded=("a",))
    cut = (BASE + timedelta(seconds=8)).isoformat()

    _, result = overview(monkeypatch, capsys, tmp_path, "--since", cut)

    assert {item["provider"] for item in rows(result)["a"]["usage"]["token_usage"]} == {"claude"}


def test_overview_usage_matches_full_usage_for_windows_and_duplicate_first_copy(
    tmp_path, monkeypatch, capsys
):
    paths, projects = register(tmp_path, "a", seeded=("a",))
    project = projects["a"]
    original = next(
        event
        for event in sample_events(project.id)
        if event.provider == "codex" and event.kind == "usage"
    )
    duplicate = original.model_copy(
        update={"event_id": uuid4(), "received_at": original.received_at + timedelta(seconds=20)}
    )
    with Store(paths.project_data(project.id), project.id) as store:
        store.put(duplicate)

    tariffs = tmp_path / "tariffs.toml"
    tariffs.write_text(TARIFFS, encoding="utf-8")
    since = (BASE + timedelta(seconds=20)).isoformat()
    until = (BASE + timedelta(seconds=30)).isoformat()
    windows = ((), ("--since", since, "--until", until))
    all_time_usage = None

    for window in windows:
        code, full_usage = cli(
            tmp_path, "usage", "--project", str(project.id), *window, "--tariffs", str(tariffs)
        )
        assert code == 0

        _, result = overview(monkeypatch, capsys, tmp_path, *window, "--tariffs", str(tariffs))
        overview_usage = rows(result)["a"]["usage"]
        assert overview_usage["token_usage"] == full_usage["token_usage"]
        assert overview_usage["pricing"] == {
            "currency": full_usage["pricing"]["currency"],
            "estimate": full_usage["pricing"]["estimate"],
            "caveat": full_usage["pricing"]["caveat"],
            "by_provider": full_usage["pricing"]["by"]["provider"],
        }
        if not window:
            all_time_usage = full_usage

    assert all_time_usage is not None
    all_time = {item["provider"]: item for item in all_time_usage["token_usage"]}
    assert all_time["codex"]["usage_rows"] == 1
    bounded = {item["provider"] for item in overview_usage["token_usage"]}
    assert "codex" not in bounded


def test_overview_queries_only_provider_usage_and_pricing(tmp_path, monkeypatch, capsys):
    register(tmp_path, "a", seeded=("a",))
    tariffs = tmp_path / "tariffs.toml"
    tariffs.write_text(TARIFFS, encoding="utf-8")
    token_groups = []
    cost_groups = []
    token_usage = facts_query.token_usage
    cost = facts_query.cost

    def record_token_group(db, *, group_by="provider", **kwargs):
        token_groups.append(group_by)
        return token_usage(db, group_by=group_by, **kwargs)

    def record_cost_group(db, tariffs, *, group_by="provider", **kwargs):
        cost_groups.append(group_by)
        return cost(db, tariffs, group_by=group_by, **kwargs)

    def unexpected(*args, **kwargs):
        raise AssertionError("overview must not query process efficiency or coverage")

    monkeypatch.setattr(facts_query, "token_usage", record_token_group)
    monkeypatch.setattr(facts_query, "cost", record_cost_group)
    monkeypatch.setattr(facts_query, "process_efficiency", unexpected)
    monkeypatch.setattr(facts_query, "coverage", unexpected)

    overview(monkeypatch, capsys, tmp_path, "--tariffs", str(tariffs))

    assert token_groups == ["provider"]
    assert cost_groups == ["provider"]


def test_usage_detailed_flag_preserves_shared_provider_results(tmp_path):
    paths, projects = register(tmp_path, "a", seeded=("a",))
    project = projects["a"]
    tariffs = tmp_path / "tariffs.toml"
    tariffs.write_text(TARIFFS, encoding="utf-8")

    from wdi.metrics.pricing import Tariffs

    full = facts_query.usage(
        registered(paths, project), since=None, until=None, tariffs=Tariffs.load(tariffs)
    )
    quick = facts_query.usage(
        registered(paths, project),
        since=None,
        until=None,
        tariffs=Tariffs.load(tariffs),
        detailed=False,
    )
    default_quick = facts_query.usage(
        registered(paths, project), since=None, until=None, detailed=False
    )

    assert {"token_by", "process", "coverage"} <= full.keys()
    assert {"token_by", "process", "coverage"}.isdisjoint(quick)
    assert {"provider", "model", "day"} <= full["pricing"]["by"].keys()
    assert quick["project_id"] == full["project_id"]
    assert quick["window"] == full["window"]
    assert quick["token_usage"] == full["token_usage"]
    assert quick["pricing"]["by"]["provider"] == full["pricing"]["by"]["provider"]
    assert default_quick["pricing"]["estimate"] is True


def test_tariffs_add_a_labeled_estimate_and_a_bad_file_fails_the_command(
    tmp_path, monkeypatch, capsys
):
    register(tmp_path, "a", seeded=("a",))
    good = tmp_path / "tariffs.toml"
    good.write_text(TARIFFS, encoding="utf-8")

    code, result = overview(monkeypatch, capsys, tmp_path, "--tariffs", str(good))

    assert code == 0
    pricing = rows(result)["a"]["usage"]["pricing"]
    assert pricing["estimate"] is True and pricing["currency"] == "USD"
    assert {item["provider"] for item in pricing["by_provider"]} == {"claude", "codex"}

    bad = tmp_path / "bad.toml"
    bad.write_text('["2026-01-01"]\n"m" = { input = "-1" }\n', encoding="utf-8")
    code, _ = overview(monkeypatch, capsys, tmp_path, "--tariffs", str(bad))
    assert code == 1


def test_overview_runs_at_a_lower_priority_than_hook_handling(tmp_path, monkeypatch, capsys):
    # The analysis is CPU-bound; a measured hook-latency regression disappeared at
    # below-normal priority (docs/verification.md, WD-138).
    register(tmp_path, "a", seeded=("a",))
    calls = []
    monkeypatch.setattr(_proc, "lower_own_priority", lambda: calls.append(True))

    overview(monkeypatch, capsys, tmp_path)

    assert calls == [True]


def test_overview_writes_nothing(tmp_path, monkeypatch, capsys):
    paths, _ = register(tmp_path, "a", "b", seeded=("a", "b"))

    def fingerprint():
        # SQLite may create empty -wal/-shm sidecars for any read-only connection.
        return {
            str(path): sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths.data.rglob("*"))
            if path.is_file() and not path.name.endswith(("-wal", "-shm"))
        }

    before = fingerprint()
    overview(monkeypatch, capsys, tmp_path)
    assert fingerprint() == before
