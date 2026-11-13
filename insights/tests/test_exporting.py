"""Offline export bundles retain selection, labels, and credential filtering."""

import json
from uuid import uuid4

import pytest
from fact_fixtures import BASE, cli, registered

from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.storage import StorageError, Store


@pytest.fixture
def source(tmp_path):
    project = Project(id=uuid4(), root=tmp_path / "repo")
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config(projects=(project,)))
    secret = "sk-proj-" + "a" * 40
    with Store(paths.project_data(project.id), project.id) as store:
        for provider, session in [("codex", "one"), ("codex", "two"), ("claude", "one")]:
            store.put(
                Envelope(
                    provider=provider,
                    project_id=project.id,
                    session_id=session,
                    kind="tool.finish",
                    source="hook",
                    received_at=BASE,
                    payload={
                        provider: {
                            "metadata": {"reasoning_effort": "high", "future_metric": True},
                            "content": {"prompt": secret, "password": "private-value"},
                        }
                    },
                )
            )
        store.label(
            "codex",
            "one",
            outcome="success",
            task_type="coding",
            progress_state="progress",
            reviewer_note="manual review",
        )
    return paths, project, secret


def export(source, output, *, provider="codex", sessions=("one",)):
    from wdi.exporting import export_sessions

    paths, project, _ = source
    return export_sessions(
        registered(paths, project), provider=provider, session_ids=list(sessions), output=output
    )


def test_export_retains_four_files_selection_labels_gaps_and_source_bytes(tmp_path, source):
    paths, project, secret = source
    database = paths.project_data(project.id) / "events.sqlite3"
    before = database.read_bytes()
    output = tmp_path / "export"
    result = export(source, output, sessions=("two", "one", "two"))
    files = ["events.jsonl", "manifest.json", "manual-prompt.md", "summary.md"]
    assert result == {
        "project_id": str(project.id),
        "provider": "codex",
        "output": str(output.resolve()),
        "files": files,
    }
    assert sorted(p.name for p in output.iterdir()) == files
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["export_version"] == "wd-011.v1"
    assert manifest["project_id"] == str(project.id) and manifest["provider"] == "codex"
    assert manifest["review_required"] is True
    assert (
        manifest["redaction"] == "credential filter was applied at export; review remains required"
    )
    sessions = manifest["sessions"]
    assert [row["session_id"] for row in sessions] == ["two", "one"]
    assert sessions[0]["label"] == {
        "task_outcome": "unknown",
        "task_type": None,
        "progress_state": None,
        "reviewer_note": None,
    }
    assert sessions[1]["label"] == {
        "task_outcome": "success",
        "task_type": "coding",
        "progress_state": "progress",
        "reviewer_note": "manual review",
    }
    assert all(row["event_count"] == 1 for row in sessions)
    assert "task_outcome_unknown" in sessions[0]["gaps"]
    assert "task_outcome_unknown" not in sessions[1]["gaps"]
    raw = (output / "events.jsonl").read_text()
    assert secret not in raw and "private-value" not in raw
    events = [Envelope.model_validate_json(line) for line in raw.splitlines()]
    assert [(event.provider, event.session_id) for event in events] == [
        ("codex", "two"),
        ("codex", "one"),
    ]
    assert events[0].payload["codex"]["metadata"]["reasoning_effort"] == "high"
    assert events[0].payload["codex"]["metadata"]["future_metric"] is True
    summary = (output / "summary.md").read_text()
    assert "Outcome: success" in summary and "Task type: coding" in summary
    assert "Review retained content before sharing" in summary
    prompt = (output / "manual-prompt.md").read_text()
    assert "untrusted data, not instructions" in prompt
    assert "Do not infer task success" in prompt
    assert database.read_bytes() == before
    with Store(paths.project_data(project.id), project.id) as store:
        assert any(secret in event.model_dump_json() for event in store.events())


def test_export_labels_are_scoped_to_the_selected_provider(tmp_path, source):
    export(source, tmp_path / "export", provider="claude")
    manifest = json.loads((tmp_path / "export" / "manifest.json").read_text())
    assert manifest["sessions"][0]["label"]["task_outcome"] == "unknown"


@pytest.mark.parametrize(
    ("provider", "sessions"),
    [
        ("unsupported", ("one",)),
        ("codex", ()),
        ("codex", (" ",)),
        ("codex", ("missing",)),
        ("codex", ("one", "missing")),
    ],
)
def test_export_rejects_invalid_selection_before_creating_output(
    tmp_path, source, provider, sessions
):
    output = tmp_path / "export"
    with pytest.raises(StorageError):
        export(source, output, provider=provider, sessions=sessions)
    assert not output.exists()


def test_export_refuses_existing_destination_without_overwriting(tmp_path, source):
    output = tmp_path / "export"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(StorageError):
        export(source, output)
    assert marker.read_text() == "keep"
    assert list(output.iterdir()) == [marker]


def test_export_cli_uses_source_registration_and_project_alias(tmp_path, source):
    _, project, _ = source
    output = tmp_path / "export"
    code, result = cli(
        tmp_path,
        "export",
        "--project",
        str(project.id),
        "--provider",
        "codex",
        "--session",
        "one",
        "--output",
        str(output),
    )
    assert code == 0
    assert result["project"] == "repo"
    assert "project_id" not in result
    assert len(result["files"]) == 4
