import json
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, UserPaths, load_config, save_config
from agent_watchdog.daemon import status, stop
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.storage import Store


def invoke(monkeypatch, capsys, home, *args):
    monkeypatch.setattr(sys, "argv", ["agent-watchdog", "--home", str(home), *args])
    code = main()
    return code, json.loads(capsys.readouterr().out)


def test_project_commands_preserve_collected_data(tmp_path, monkeypatch, capsys):
    home, root = tmp_path / "state", tmp_path / "source"
    root.mkdir()
    code, project = invoke(monkeypatch, capsys, home, "project", "add", str(root))
    assert code == 0
    assert project["project"] == "source"
    assert "id" not in project
    project_id = load_config(home / "config.toml").projects[0].id
    data = home / "data" / "projects" / str(project_id)
    data.mkdir(parents=True)
    sentinel = data / "preserve.txt"
    sentinel.write_text("collected")
    code, listed = invoke(monkeypatch, capsys, home, "project", "list")
    assert code == 0 and listed["projects"][0]["project"] == "source"
    new_root = tmp_path / "moved"
    root.rename(new_root)
    code, moved = invoke(monkeypatch, capsys, home, "project", "relocate", "source", str(new_root))
    assert code == 0 and moved["project"] == "moved"
    assert moved["root"] == str(new_root)
    code, _ = invoke(monkeypatch, capsys, home, "project", "remove", "moved")
    assert code == 0 and sentinel.read_text() == "collected"
    _, listed = invoke(monkeypatch, capsys, home, "project", "list")
    assert listed["projects"] == []


def test_project_aliases_are_case_sensitive_and_resolve_duplicates_cwd_and_uuid_input(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "state"
    first = tmp_path / "first" / "Project"
    second = tmp_path / "second" / "project"
    third = tmp_path / "third" / "Project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    third.mkdir(parents=True)
    code, added_first = invoke(monkeypatch, capsys, home, "project", "add", str(first))
    assert code == 0 and added_first["project"] == "Project"
    code, added_second = invoke(monkeypatch, capsys, home, "project", "add", str(second))
    assert code == 0 and added_second["project"] == "project"
    code, added_third = invoke(monkeypatch, capsys, home, "project", "add", str(third))
    assert code == 0 and added_third["project"] == "Project-2"
    projects = load_config(home / "config.toml").projects
    first_project, second_project, third_project = projects
    for project, session_id in zip(
        projects, ("first-session", "second-session", "third-session"), strict=True
    ):
        with Store(home / "data" / "projects" / str(project.id), project.id) as store:
            store.put(
                Envelope(
                    provider="codex",
                    project_id=project.id,
                    session_id=session_id,
                    kind="turn.end",
                    source="hook",
                )
            )
    code, listed = invoke(monkeypatch, capsys, home, "sessions", "list", "--project", "Project")
    assert code == 0 and listed["project"] == "Project"
    assert listed["sessions"][0]["session_id"] == "first-session"
    code, listed = invoke(monkeypatch, capsys, home, "sessions", "list", "--project", "project")
    assert code == 0 and listed["project"] == "project"
    assert listed["sessions"][0]["session_id"] == "second-session"
    code, listed = invoke(monkeypatch, capsys, home, "sessions", "list", "--project", "Project-2")
    assert code == 0 and listed["project"] == "Project-2"
    assert listed["sessions"][0]["session_id"] == "third-session"
    code, error = invoke(monkeypatch, capsys, home, "sessions", "list", "--project", "PROJECT")
    assert code == 1 and error["error"] == "StorageError"
    code, listed = invoke(
        monkeypatch, capsys, home, "sessions", "list", "--project", str(second_project.id)
    )
    assert code == 0 and listed["project"] == "project"
    monkeypatch.chdir(first)
    code, listed = invoke(monkeypatch, capsys, home, "sessions", "list")
    assert code == 0 and listed["project"] == "Project"
    code, removed = invoke(monkeypatch, capsys, home, "project", "remove", "Project-2")
    assert code == 0 and removed["removed"] == "Project-2"
    assert third_project.id not in {
        project.id for project in load_config(home / "config.toml").projects
    }


def test_failed_command_log_names_the_error_category(tmp_path, monkeypatch, capsys):
    home = tmp_path / "state"
    code, error = invoke(monkeypatch, capsys, home, "sessions", "list", "--project", "missing")
    assert code == 1 and error["error"] == "StorageError"
    body = (home / "data" / "watchdog.log").read_text(encoding="ascii")
    assert "component=cli event=command decision=failed error_type=storageerror" in body


def test_doctor_does_not_initialize_fresh_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "absent"
    code, report = invoke(monkeypatch, capsys, home, "doctor")
    assert code == 0
    assert report["projects"] == []
    assert report["daemon"]["state"] == "unavailable"
    assert "native_hook_trust_not_inspected" in report["gaps"]
    assert not home.exists()


def test_sessions_separate_identities_and_report_gaps(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    with Store(data, project.id) as store:
        samples: list[tuple[str | None, EventKind]] = [
            ("one", "session.start"),
            ("one", "turn.end"),
            ("two", "tool.finish"),
            (None, "unknown"),
        ]
        for session, kind in samples:
            payload = (
                {"codex": {"content": {"prompt": "password=private-value"}}}
                if session == "one" and kind == "session.start"
                else {}
            )
            store.put(
                Envelope(
                    provider="codex",
                    project_id=project.id,
                    session_id=session,
                    kind=kind,
                    source="hook",
                    payload=payload,
                )
            )
    code, result = invoke(
        monkeypatch, capsys, tmp_path, "sessions", "list", "--project", str(project.id)
    )
    assert code == 0 and len(result["sessions"]) == 3
    assert result["project"] == tmp_path.name
    one = next(item for item in result["sessions"] if item["session_id"] == "one")
    assert one["event_count"] == 2 and "session_end_not_observed" in one["gaps"]
    assert one["task_outcome"] == "unknown"
    with Store(data, project.id):
        code, result = invoke(
            monkeypatch,
            capsys,
            tmp_path,
            "sessions",
            "show",
            "one",
            "--project",
            str(project.id),
            "--limit",
            "10",
        )
    assert code == 0 and len(result["events"]) == 2 and not result["has_more"]
    assert all(item["session_id"] == "one" for item in result["events"])
    assert "private-value" in json.dumps(result["events"])


def test_summary_counts_known_data_and_leaves_missing_database_unknown(
    tmp_path, monkeypatch, capsys
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = Project(id=uuid4(), root=first_root)
    second = Project(id=uuid4(), root=second_root)
    save_config(tmp_path / "config.toml", Config(projects=(first, second)))
    data = tmp_path / "data" / "projects" / str(first.id)
    with Store(data, first.id) as store:
        for provider, session_id, kind in (
            ("codex", "one", "session.start"),
            ("codex", "one", "turn.end"),
            ("claude", "one", "tool.finish"),
            ("codex", None, "unknown"),
        ):
            store.put(
                Envelope(
                    provider=provider,
                    project_id=first.id,
                    session_id=session_id,
                    kind=kind,
                    source="hook",
                )
            )
    code, result = invoke(monkeypatch, capsys, tmp_path, "summary")
    assert code == 0
    assert result["ok"] is True and result["complete"] is False
    assert result["observed_session_count"] == 2
    assert result["observed_event_count"] == 4
    first_summary, second_summary = result["projects"]
    assert first_summary == {
        "project": "first",
        "root": str(first_root),
        "database": {"state": "ready"},
        "session_count": 2,
        "event_count": 4,
    }
    assert second_summary["database"] == {"state": "unavailable"}
    assert second_summary["session_count"] is None and second_summary["event_count"] is None


def test_summary_reports_corrupt_database_as_partial_error(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    data.mkdir(parents=True)
    (data / "events.sqlite3").write_bytes(b"not a database")
    code, result = invoke(monkeypatch, capsys, tmp_path, "summary")
    assert code == 1
    assert result["ok"] is False and result["complete"] is False
    assert result["projects"][0]["database"] == {"state": "error", "error": "DatabaseError"}
    assert result["projects"][0]["session_count"] is None
    assert result["observed_event_count"] == 0


def test_sessions_hide_usage_gap_after_transcript_enrichment(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    with Store(data, project.id) as store:
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                session_id="one",
                kind="usage",
                source="transcript",
                availability={"input_tokens": "observed"},
            )
        )
    code, result = invoke(
        monkeypatch, capsys, tmp_path, "sessions", "list", "--project", str(project.id)
    )
    assert code == 0
    assert "usage_not_enriched" not in result["sessions"][0]["gaps"]


def test_report_is_read_only_and_returns_shadow_findings(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    with Store(data, project.id) as store:
        for _ in range(3):
            store.put(
                Envelope(
                    provider="codex",
                    project_id=project.id,
                    session_id="one",
                    kind="tool.finish",
                    source="hook",
                    payload={
                        "codex": {
                            "tool_name": "exec",
                            "content": {
                                "tool_input": {"command": "pytest tests/test_sample.py"},
                                "tool_response": {
                                    "exit_code": 1,
                                    "output": (
                                        "FAILED tests/test_sample.py::test_x - AssertionError"
                                    ),
                                },
                            },
                        }
                    },
                )
            )
    before = (data / "events.sqlite3").read_bytes()
    code, report = invoke(
        monkeypatch, capsys, tmp_path, "report", "--project", str(project.id), "--session", "one"
    )
    assert code == 0
    assert {item["rule"] for item in report["findings"]} == {
        "identical_error",
        "repeated_test_failure",
        "repeated_tool_outcome",
    }
    assert report["session_ids"] == ["one"]
    assert report["project"] == tmp_path.name
    assert (data / "events.sqlite3").read_bytes() == before


def test_diff_oscillation_is_not_reported_for_an_unrelated_session_sharing_a_checkout(
    tmp_path, monkeypatch, capsys
):
    # WD-118: a checkout-wide oscillation must not fan out to every session that
    # ever touched the checkout; only the session whose turn was open when the
    # A-to-B-to-A snapshots were captured should see it.
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    checkout = uuid4()
    start = datetime(2026, 9, 16, tzinfo=UTC)
    with Store(data, project.id) as store:
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                checkout_id=checkout,
                session_id="implicated",
                kind="turn.start",
                source="hook",
                received_at=start,
            )
        )
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                checkout_id=checkout,
                session_id="implicated",
                kind="turn.end",
                source="hook",
                received_at=start + timedelta(seconds=10),
            )
        )
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                checkout_id=checkout,
                session_id="bystander",
                kind="turn.start",
                source="hook",
                received_at=start + timedelta(hours=1),
            )
        )
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                checkout_id=checkout,
                session_id="bystander",
                kind="turn.end",
                source="hook",
                received_at=start + timedelta(hours=1, seconds=10),
            )
        )
        store.record_diff_snapshot(checkout, "1" * 64, 10, observed_at=start + timedelta(seconds=1))
        store.record_diff_snapshot(checkout, "2" * 64, 10, observed_at=start + timedelta(seconds=2))
        store.record_diff_snapshot(checkout, "1" * 64, 10, observed_at=start + timedelta(seconds=3))

    code, implicated_report = invoke(
        monkeypatch,
        capsys,
        tmp_path,
        "report",
        "--project",
        str(project.id),
        "--session",
        "implicated",
    )
    assert code == 0
    assert {item["rule"] for item in implicated_report["findings"]} == {"diff_oscillation"}

    code, bystander_report = invoke(
        monkeypatch,
        capsys,
        tmp_path,
        "report",
        "--project",
        str(project.id),
        "--session",
        "bystander",
    )
    assert code == 0
    assert bystander_report["findings"] == []


def test_label_pin_export_and_purge_are_offline_and_provider_scoped(tmp_path, monkeypatch, capsys):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    data = tmp_path / "data" / "projects" / str(project.id)
    with Store(data, project.id) as store:
        for provider in ("codex", "claude"):
            store.put(
                Envelope(
                    provider=provider,
                    project_id=project.id,
                    session_id="shared",
                    kind="turn.end",
                    source="hook",
                    payload={provider: {"content": {"prompt": "review locally"}}},
                )
            )
    try:
        code, label = invoke(
            monkeypatch,
            capsys,
            tmp_path,
            "label",
            "shared",
            "--project",
            str(project.id),
            "--outcome",
            "success",
            "--task-type",
            "bugfix",
        )
        assert code == 0, label
        assert label["project"] == tmp_path.name
        assert label["label"] == {
            "task_outcome": "success",
            "task_type": "bugfix",
            "progress_state": None,
            "reviewer_note": None,
        }

        code, pin = invoke(
            monkeypatch, capsys, tmp_path, "pin", "shared", "--project", str(project.id)
        )
        assert code == 0 and pin["project"] == tmp_path.name and pin["pinned"] is True, pin

        code, shown = invoke(
            monkeypatch,
            capsys,
            tmp_path,
            "sessions",
            "show",
            "shared",
            "--project",
            str(project.id),
        )
        assert code == 0, shown
        assert shown["label"] == {
            "task_outcome": "success",
            "task_type": "bugfix",
            "progress_state": None,
            "reviewer_note": None,
        }
        assert shown["pinned"] is True

        output = tmp_path / "export"
        code, exported = invoke(
            monkeypatch,
            capsys,
            tmp_path,
            "export",
            "--project",
            str(project.id),
            "--session",
            "shared",
            "--output",
            str(output),
        )
        assert code == 0
        assert exported["project"] == tmp_path.name
        assert set(exported["files"]) == {
            "events.jsonl",
            "manifest.json",
            "manual-prompt.md",
            "summary.md",
        }
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["schema_version"] == 1
        assert manifest["review_required"] is True
        assert manifest["sessions"][0]["gaps"] == ["active_time_unknown", "usage_incomplete"]
        assert "review the exported content" in (output / "manual-prompt.md").read_text().lower()

        code, purged = invoke(
            monkeypatch, capsys, tmp_path, "purge", "shared", "--project", str(project.id)
        )
        assert code == 0 and purged["project"] == tmp_path.name and purged["deleted_events"] == 1
        with Store(data, project.id) as store:
            assert [event.provider for event in store.events()] == ["claude"]
    finally:
        stop(paths)
        deadline = time.monotonic() + 10
        while status(paths)["alive"]:
            if time.monotonic() >= deadline:
                pytest.fail("Timed out stopping daemon")
            time.sleep(0.05)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "future", "wrong_project"])
def test_doctor_distinguishes_database_failures(tmp_path, monkeypatch, capsys, fault):
    project = Project(id=uuid4(), root=tmp_path)
    save_config(tmp_path / "config.toml", Config(projects=(project,)))
    data = tmp_path / "data" / "projects" / str(project.id)
    if fault == "corrupt":
        data.mkdir(parents=True)
        (data / "events.sqlite3").write_bytes(b"not a database")
    elif fault != "missing":
        with Store(data, project.id) as store:
            if fault == "future":
                store.connection.execute("PRAGMA user_version=99")
            else:
                store.connection.execute("UPDATE metadata SET project_id=?", (str(uuid4()),))
    code, report = invoke(monkeypatch, capsys, tmp_path, "doctor")
    assert report["projects"][0]["project"] == tmp_path.name
    assert report["projects"][0]["database"]["state"] == (
        "unavailable" if fault == "missing" else "error"
    )
    assert code == (0 if fault == "missing" else 1)
