"""HTTP acceptance for captured environment and repository outcome panels."""

import json
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wdi.config import Settings
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.web import create_app


@pytest.fixture
def web(tmp_path: Path) -> tuple[TestClient, Path]:
    path = tmp_path / "insights.sqlite"
    with closing(connect(path)) as conn:
        repo = Repository(conn)
        repo.ensure_machine("machine")
        project = repo.get_or_create_project("demo")
        conn.execute(
            "INSERT INTO sessions (session_id, machine_id, project_id, agent, title, started_at) "
            "VALUES ('before', 'machine', ?, 'codex', 'Before snapshot', '2026-01-01'), "
            "('current', 'machine', ?, 'codex', 'Current snapshot', '2026-01-02')",
            (project, project),
        )
        conn.commit()
    return TestClient(create_app(Settings(machine_id="machine", db_path=path))), path


def _snapshots(path: Path, *, unavailable: bool = False) -> None:
    with closing(connect(path)) as conn:
        for sid, digest, captured in [
            ("before", "old", "2026-01-01"),
            ("current", "new", "2026-01-02"),
        ]:
            conn.execute(
                "INSERT INTO session_env "
                "(machine_id, project_id, store_path, provider, session_id, captured_at, "
                "cwd, checkout_id, head, branch, harness, files, watchdog, initial_diff_digest) "
                "VALUES ('machine', 'source-project', '/unavailable/store', 'codex', ?, ?, "
                "'/demo', 'checkout', 'abc123', 'feature/demo', ?, ?, ?, 'initial-digest')",
                (
                    sid,
                    captured,
                    json.dumps({"model": "demo-model"}),
                    json.dumps(
                        [
                            {"path": "AGENTS.md", "role": "instruction", "digest": digest},
                            {"path": "settings.json", "role": "settings", "digest": "settings"},
                        ]
                    ),
                    json.dumps({"rules": [{"name": "demo-rule", "status": "approved"}]}),
                ),
            )
        conn.execute(
            "INSERT INTO checkout_states "
            "(machine_id, project_id, store_path, provider, session_id, checkout_id, "
            "observed_at, trigger, base_head, head, branch, files_changed, insertions, "
            "deletions, untracked, diff_digest, error) "
            "VALUES ('machine', 'source-project', '/unavailable/store', 'codex', 'current', "
            "'checkout', '2026-01-03', 'stop', 'abc123', 'def456', 'feature/demo', "
            "?, ?, ?, ?, ?, ?)",
            (None, None, None, None, None, "git_unavailable")
            if unavailable
            else (2, 14, 3, 0, "outcome-digest", None),
        )
        conn.commit()


def test_missing_snapshots_render_unavailable_panels(web: tuple[TestClient, Path]) -> None:
    client, _ = web
    response = client.get("/sessions/current")
    assert response.status_code == 200
    assert 'id="environment"' in response.text
    assert "Environment unavailable" in response.text
    assert 'id="outcome"' in response.text
    assert "Outcome unavailable" in response.text


def test_panels_show_files_rules_and_observed_outcome(web: tuple[TestClient, Path]) -> None:
    client, path = web
    _snapshots(path)
    response = client.get("/sessions/current")
    assert response.status_code == 200
    for value in (
        "/demo",
        "abc123",
        "feature/demo",
        "demo-model",
        "AGENTS.md",
        "settings.json",
        "Changed since previous session",
        "demo-rule",
        "Files changed",
        "Insertions",
        "Deletions",
        "HEAD moved",
        "Observed checkout state",
        "View captured diff",
    ):
        assert value in response.text
    assert "/sessions/current/diff/outcome" in response.text
    assert "/sessions/current/diff/initial" in response.text


def test_capture_error_keeps_statistics_unavailable(web: tuple[TestClient, Path]) -> None:
    client, path = web
    _snapshots(path, unavailable=True)
    response = client.get("/sessions/current")
    assert response.status_code == 200
    assert "git_unavailable" in response.text
    assert "Unavailable" in response.text


@pytest.mark.parametrize("hx", [False, True])
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("branch=feature%2Fdemo", True),
        ("branch=missing", False),
        ("repository_changed=true", True),
        ("instructions_changed=true", True),
    ],
)
def test_snapshot_filters_apply_to_session_list(
    web: tuple[TestClient, Path], hx: bool, query: str, expected: bool
) -> None:
    client, path = web
    _snapshots(path)
    response = client.get(
        "/sessions?" + query,
        headers={"HX-Request": "true"} if hx else {},
    )
    assert response.status_code == 200
    assert ('href="/sessions/current"' in response.text) is expected
    assert ("<!DOCTYPE html>" in response.text) is not hx
    if query == "instructions_changed=true":
        assert 'href="/sessions/before"' not in response.text


def test_filter_controls_preserve_selected_values(web: tuple[TestClient, Path]) -> None:
    client, _ = web
    response = client.get(
        "/sessions?branch=feature%2Fdemo&repository_changed=true&instructions_changed=false"
    )
    assert response.status_code == 200
    assert 'name="branch"' in response.text
    assert 'value="feature/demo"' in response.text
    assert 'name="repository_changed"' in response.text
    assert 'name="instructions_changed"' in response.text
    assert 'value="true" selected' in response.text
    assert 'value="false" selected' in response.text


def test_invalid_boolean_filter_is_rejected(web: tuple[TestClient, Path]) -> None:
    client, _ = web
    assert client.get("/sessions?repository_changed=garbage").status_code == 422


def test_unknown_session_diff_is_not_found(web: tuple[TestClient, Path]) -> None:
    client, _ = web
    assert client.get("/sessions/missing/diff/outcome").status_code == 404


def test_missing_blob_renders_actionable_unavailable_state(web: tuple[TestClient, Path]) -> None:
    client, path = web
    _snapshots(path)
    response = client.get("/sessions/current/diff/outcome")
    assert response.status_code == 200
    assert "Diff unavailable" in response.text
    assert "/unavailable/store" not in response.text


def test_diff_content_is_escaped(
    web: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdi.metrics import environment

    monkeypatch.setattr(
        environment,
        "read_session_diff",
        lambda *_args, **_kwargs: {"text": "+<script>alert(1)</script>", "reason": None},
    )
    client, _ = web
    response = client.get("/sessions/current/diff/initial")
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "+<script>" not in response.text


def test_empty_file_and_rule_lists_are_explicit(web: tuple[TestClient, Path]) -> None:
    client, path = web
    _snapshots(path)
    with closing(connect(path)) as conn:
        conn.execute("UPDATE session_env SET files = '[]', watchdog = '{}' ")
        conn.execute("DELETE FROM checkout_states")
        conn.commit()
    response = client.get("/sessions/current")
    assert response.status_code == 200
    assert "No instruction or settings files captured" in response.text
    assert "Rule set unavailable" in response.text
    assert "Outcome unavailable" in response.text


def test_snapshot_filter_links_preserve_false_and_branch() -> None:
    from wdi.metrics.filters import SessionFilter
    from wdi.web.filters import filter_query

    query = filter_query(
        SessionFilter(branch="feature/demo", repository_changed=True, instructions_changed=False)
    )
    assert "branch=feature%2Fdemo" in query
    assert "repository_changed=true" in query
    assert "instructions_changed=false" in query


def test_snapshot_filters_keep_coach_cache_distinct() -> None:
    from wdi.llm.coach import filter_hash
    from wdi.metrics.filters import SessionFilter

    filters = [
        SessionFilter(),
        SessionFilter(branch="main"),
        SessionFilter(repository_changed=True),
        SessionFilter(repository_changed=False),
        SessionFilter(instructions_changed=True),
        SessionFilter(instructions_changed=False),
    ]
    assert len({filter_hash(value) for value in filters}) == len(filters)


def test_initial_diff_link_survives_later_environment_snapshot(
    web: tuple[TestClient, Path],
) -> None:
    client, path = web
    _snapshots(path)
    with closing(connect(path)) as conn:
        conn.execute(
            "INSERT INTO session_env "
            "(machine_id, project_id, store_path, provider, session_id, captured_at, "
            "cwd, checkout_id, head, branch, harness, files, watchdog) "
            "SELECT machine_id, project_id, store_path, provider, session_id, '2026-01-04', "
            "cwd, checkout_id, head, branch, harness, files, watchdog "
            "FROM session_env WHERE session_id = 'current'"
        )
        conn.commit()
    response = client.get("/sessions/current")
    assert response.status_code == 200
    assert "/sessions/current/diff/initial" in response.text
