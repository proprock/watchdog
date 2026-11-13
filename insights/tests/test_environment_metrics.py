"""Captured environment metrics behavior."""

import json
from contextlib import closing
from pathlib import Path

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.metrics import queries
from wdi.metrics.filters import SessionFilter


@pytest.fixture
def db(tmp_path: Path):
    with closing(connect(tmp_path / "metrics.sqlite")) as conn:
        repo = Repository(conn)
        repo.ensure_machine("m")
        repo.ensure_machine("other")
        project = repo.get_or_create_project("demo")
        for name, started in [
            ("first", "2026-01-01"),
            ("second", "2026-01-02"),
            ("third", "2026-01-03"),
        ]:
            conn.execute(
                """INSERT INTO sessions (session_id, machine_id, project_id, agent,
                   started_at, git_branch) VALUES (?, 'm', ?, 'codex', ?, 'transcript')""",
                (name, project, started),
            )
        yield conn


def env(db, session, captured, files, *, branch="main"):
    db.execute(
        """INSERT INTO session_env (machine_id, project_id, store_path, provider, session_id,
           captured_at, checkout_id, branch, harness, files, watchdog)
           VALUES ('m', 'source', '/store', 'codex', ?, ?, 'c', ?, ?, ?, ?)""",
        (
            session,
            captured,
            branch,
            json.dumps({"model": "model"}),
            json.dumps(files),
            json.dumps({"rules": [{"name": "rule"}]}),
        ),
    )


def outcome(
    db, session, observed, *, machine="m", checkout="c", base="a", head="a", files=0, untracked=0
):
    db.execute(
        """INSERT INTO checkout_states (machine_id, project_id, store_path, checkout_id,
           observed_at, provider, session_id, trigger, base_head, head, files_changed, untracked)
           VALUES (?, 'source', '/store', ?, ?, 'codex', ?, 'stop', ?, ?, ?, ?)""",
        (machine, checkout, observed, session, base, head, files, untracked),
    )


def file(digest, path="instructions", error=None):
    return {"path": path, "role": "instruction", "digest": digest, "error": error}


def panels(db, session):
    from wdi.metrics.environment import session_panels

    return session_panels(db, session)


def test_empty_and_first_session_are_unknown(db):
    assert panels(db, "first")["environment"] is None
    env(db, "first", "1", [file("a")])
    value = panels(db, "first")
    assert value["outcome"] is None
    assert value["instructions_changed"] is None
    assert value["files"][0]["changed"] is None
    assert value["environment"]["harness"] == {"model": "model"}
    assert value["rules"] == [{"name": "rule"}]


def test_actual_predecessor_and_late_snapshots(db):
    env(db, "first", "1", [file("a")])
    env(db, "second", "1", [file("a")])
    env(db, "second", "9", [file("b")])
    env(db, "third", "1", [file("b")])
    assert panels(db, "second")["instructions_changed"] is True
    assert panels(db, "third")["instructions_changed"] is False
    env(db, "third", "9", [file(None, error="unreadable")])
    assert panels(db, "third")["instructions_changed"] is None


def test_added_removed_files_are_changes(db):
    env(db, "first", "1", [file("a"), file("x", "removed")])
    env(db, "second", "1", [file("a"), file("b", "added")])
    value = panels(db, "second")
    assert value["instructions_changed"] is True
    assert {item["path"] for item in value["files"] if item["changed"]} == {"removed", "added"}


def test_outcome_is_scoped_to_snapshot_source_and_checkout(db):
    env(db, "second", "1", [])
    outcome(db, "second", "1")
    outcome(db, "second", "9", machine="other", head="other")
    outcome(db, "second", "8", checkout="wrong", head="wrong")
    assert panels(db, "second")["outcome"]["head"] == "a"
    assert panels(db, "second")["head_moved"] is False
    outcome(db, "second", "10", head="b")
    assert panels(db, "second")["head_moved"] is True


def test_watchdog_session_uses_its_transcript_project(db):
    db.execute(
        "UPDATE sessions SET origin = 'watchdog', source_path = ? WHERE session_id = 'second'",
        ("wd://m/source/codex/second/stream",),
    )
    env(db, "second", "1", [file("a")], branch="selected")
    db.execute(
        """INSERT INTO session_env (machine_id, project_id, store_path, provider, session_id,
           captured_at, checkout_id, branch, harness, files, watchdog)
           VALUES ('m', 'other-source', '/other', 'codex', 'second', '9', 'c', 'wrong',
                   '{}', '[]', '{}')"""
    )
    assert panels(db, "second")["environment"]["branch"] == "selected"
    assert [
        row["session_id"] for row in queries.session_list(db, SessionFilter(branch="selected"))
    ] == ["second"]
    assert queries.session_list(db, SessionFilter(branch="wrong")) == []


def test_filters_apply_before_limit_and_keep_unknown_distinct(db):
    env(db, "first", "1", [file("a")], branch="feature")
    env(db, "second", "1", [file("b")])
    outcome(db, "first", "1", files=1)
    outcome(db, "second", "1", files=0)
    assert [
        r["session_id"] for r in queries.session_list(db, SessionFilter(branch="feature"), limit=1)
    ] == ["first"]
    assert [
        r["session_id"] for r in queries.session_list(db, SessionFilter(branch="transcript"))
    ] == ["third"]
    assert [
        r["session_id"] for r in queries.session_list(db, SessionFilter(repository_changed=True))
    ] == ["first"]
    assert [
        r["session_id"] for r in queries.session_list(db, SessionFilter(repository_changed=False))
    ] == ["second"]
    assert [
        r["session_id"] for r in queries.session_list(db, SessionFilter(instructions_changed=True))
    ] == ["second"]
    assert queries.session_list(db, SessionFilter(instructions_changed=False)) == []


def test_diff_absent_and_untrusted_store_are_unavailable(db, tmp_path):
    from wdi.metrics.environment import read_session_diff

    env(db, "first", "1", [])
    result = read_session_diff(db, "first", "initial", [tmp_path])
    assert result["text"] is None
    assert result["reason"]
    assert str(tmp_path) not in result["reason"]


def test_diff_reads_only_the_configured_matching_store(db, tmp_path):
    import hashlib
    from uuid import uuid4

    from wdi.metrics.environment import read_session_diff

    from agent_watchdog import capture_store, config, readapi

    machine = uuid4()
    project = config.Project(id=uuid4(), root=tmp_path)
    data = tmp_path / "data"
    directory = data / "projects" / str(project.id)
    directory.mkdir(parents=True)
    capture_store.write_manifest(directory, machine_id=machine, project=project)
    store = capture_store.CaptureStore(directory / capture_store.CAPTURE_FILENAME)
    content = b"captured diff\n"
    digest = hashlib.sha256(content).hexdigest()
    snapshot = capture_store.SessionEnvironment(
        provider="codex",
        session_id="first",
        captured_at="2026-01-01",
        cwd=None,
        checkout_id="c",
        head=None,
        branch=None,
        harness={},
        files=[],
        watchdog={},
        initial_diff_digest=digest,
    )
    store.record_session_env(snapshot, {digest: content}, capture_content=True)
    Repository(db).ensure_machine(str(machine))
    db.execute("UPDATE sessions SET machine_id = ? WHERE session_id = 'first'", (str(machine),))
    info = readapi.stores(data)[0]
    source = list(readapi.session_env(info))[0]
    db.execute(
        """INSERT INTO session_env (machine_id, project_id, store_path, provider, session_id,
        captured_at, checkout_id, harness, files, watchdog, initial_diff_digest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(machine),
            str(project.id),
            str(directory.resolve()),
            source["provider"],
            source["session_id"],
            source["captured_at"],
            source["checkout_id"],
            source["harness"],
            source["files"],
            source["watchdog"],
            source["initial_diff_digest"],
        ),
    )
    db.execute(
        """INSERT INTO session_env (machine_id, project_id, store_path, provider, session_id,
           captured_at, checkout_id, harness, files, watchdog)
           SELECT machine_id, project_id, store_path, provider, session_id, '2026-02-01',
           checkout_id, harness, files, watchdog FROM session_env WHERE session_id = 'first'"""
    )
    assert read_session_diff(db, "first", "initial", [data]) == {
        "text": "captured diff\n",
        "reason": None,
    }
    assert read_session_diff(db, "first", "initial", [tmp_path / "wrong"])["text"] is None
    with store.connect() as capture:
        capture.execute("DELETE FROM blobs")
    missing = read_session_diff(db, "first", "initial", [data])
    assert missing["text"] is None
    assert missing["reason"] and str(tmp_path) not in missing["reason"]
