"""Offline contracts for the read-only cross-project `overview` command (WD-016)."""

import json
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from test_facts_query import seed
from test_insights import _finish
from test_pricing import TARIFFS
from test_storage_v6 import BASE, build_v5

from agent_watchdog import inspection
from agent_watchdog.cli import main
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


def overview(monkeypatch, capsys, tmp_path, *args):
    monkeypatch.setattr(sys, "argv", ["agent-watchdog", "--home", str(tmp_path), "overview", *args])
    code = main()
    return code, json.loads(capsys.readouterr().out)


def rows(result):
    return {row["project"]: row for row in result["projects"]}


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
        assert "pricing" not in row["usage"]


def test_findings_count_a_fired_rule_per_provider(tmp_path, monkeypatch, capsys):
    paths, projects = register(tmp_path, "loopy")
    project = projects["loopy"]
    with Store(paths.project_data(project.id), project.id) as store:
        for at in range(3):
            store.put(_finish(project.id, at=at))

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
    return events


def _expected_per_provider(paths, project):
    expected = {}
    for provider in ("codex", "claude"):
        found = inspection.report(paths, project, session_id=None, provider=provider)
        expected[provider] = {
            "by_rule": dict(Counter(finding["rule"] for finding in found["findings"])),
            "gaps": found["gaps"],
        }
    return expected


def test_overview_findings_equal_the_per_provider_report(tmp_path, monkeypatch, capsys):
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

    _, result = overview(monkeypatch, capsys, tmp_path)

    found = rows(result)["noisy"]["findings"]["by_provider"]
    assert found == _expected_per_provider(paths, project)
    assert "repeated_tool_outcome" in found["claude"]["by_rule"], found
    assert "repeated_tool_outcome" in found["codex"]["by_rule"], found
    assert "diff_oscillation" in found["codex"]["by_rule"], found


def test_overview_findings_on_a_pre_v6_store_equal_the_per_provider_report(
    tmp_path, monkeypatch, capsys
):
    paths, projects = register(tmp_path, "old")
    project = projects["old"]
    root = paths.project_data(project.id)
    root.mkdir(parents=True)
    build_v5(root, project.id, _noisy_events(project, uuid4()))

    _, result = overview(monkeypatch, capsys, tmp_path)

    found = rows(result)["old"]["findings"]["by_provider"]
    assert found == _expected_per_provider(paths, project)
    assert found["claude"]["by_rule"] and found["codex"]["by_rule"], found


def test_each_event_is_parsed_once_and_never_inside_the_read_snapshot(tmp_path, monkeypatch):
    paths, projects = register(tmp_path, "noisy")
    project = projects["noisy"]
    events = _noisy_events(project, uuid4())
    with Store(paths.project_data(project.id), project.id) as store:
        for event in events:
            store.put(event)
    open_snapshots = 0
    parsed = 0
    real_database, real_parse = inspection.database, inspection.persisted_envelope

    @contextmanager
    def tracked(*args):
        nonlocal open_snapshots
        open_snapshots += 1
        try:
            with real_database(*args) as db:
                yield db
        finally:
            open_snapshots -= 1

    def counting(document):
        nonlocal parsed
        assert open_snapshots == 0, "parsing must not hold the read snapshot open"
        parsed += 1
        return real_parse(document)

    monkeypatch.setattr(inspection, "database", tracked)
    monkeypatch.setattr(inspection, "persisted_envelope", counting)

    inspection.overview(paths, since=None, until=None)

    assert parsed == len(events)


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
    assert old["findings"]["state"] == "ready"
    assert rows(result)["new"]["usage"]["state"] == "ready"


def test_since_bounds_usage(tmp_path, monkeypatch, capsys):
    register(tmp_path, "a", seeded=("a",))
    cut = (BASE + timedelta(seconds=8)).isoformat()

    _, result = overview(monkeypatch, capsys, tmp_path, "--since", cut)

    assert {item["provider"] for item in rows(result)["a"]["usage"]["token_usage"]} == {"claude"}


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
