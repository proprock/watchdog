import json
import sys
import time
from uuid import uuid4

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import Config, Project, UserPaths, save_config
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
    project_id = project["id"]
    data = home / "data" / "projects" / project_id
    data.mkdir(parents=True)
    sentinel = data / "preserve.txt"
    sentinel.write_text("collected")
    code, listed = invoke(monkeypatch, capsys, home, "project", "list")
    assert code == 0 and listed["projects"][0]["id"] == project_id
    new_root = tmp_path / "moved"
    root.rename(new_root)
    code, moved = invoke(
        monkeypatch, capsys, home, "project", "relocate", project_id, str(new_root)
    )
    assert code == 0 and moved["id"] == project_id
    assert moved["root"] == str(new_root)
    code, _ = invoke(monkeypatch, capsys, home, "project", "remove", project_id)
    assert code == 0 and sentinel.read_text() == "collected"
    _, listed = invoke(monkeypatch, capsys, home, "project", "list")
    assert listed["projects"] == []


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
            store.put(
                Envelope(
                    provider="codex",
                    project_id=project.id,
                    session_id=session,
                    kind=kind,
                    source="hook",
                )
            )
    code, result = invoke(
        monkeypatch, capsys, tmp_path, "sessions", "list", "--project", str(project.id)
    )
    assert code == 0 and len(result["sessions"]) == 3
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
            "1",
        )
    assert code == 0 and len(result["events"]) == 1 and result["has_more"]
    assert result["events"][0]["session_id"] == "one"


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
    assert (data / "events.sqlite3").read_bytes() == before


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
        assert label["label"] == {"task_outcome": "success", "task_type": "bugfix"}

        code, pin = invoke(
            monkeypatch, capsys, tmp_path, "pin", "shared", "--project", str(project.id)
        )
        assert code == 0 and pin["pinned"] is True, pin

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
        assert shown["label"] == {"task_outcome": "success", "task_type": "bugfix"}
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
        assert code == 0 and purged["deleted_events"] == 1
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
    assert report["projects"][0]["database"]["state"] == (
        "unavailable" if fault == "missing" else "error"
    )
    assert code == (0 if fault == "missing" else 1)
