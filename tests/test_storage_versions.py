"""WD-162: settled sessions must follow analysis and reducer releases."""

import json
import sqlite3
from dataclasses import replace

import pytest
from test_storage_v8 import PROJECT, _repeat

from agent_watchdog import analysis, state
from agent_watchdog.daemon import _SessionTracker
from agent_watchdog.storage import Store


@pytest.mark.parametrize("count", [1, 3])
def test_rule_release_refreshes_settled_sessions(tmp_path, monkeypatch, count):
    with Store(tmp_path, PROJECT) as store:
        _repeat(store, "s1", count)
        store.refresh_session_findings("codex", "s1")
        old = store.connection.execute("SELECT fingerprint FROM session_findings").fetchall()
        assert store.pending_sessions() == []

    monkeypatch.setattr(analysis, "RULE_VERSION", "wd-010.test-release")
    with Store(tmp_path, PROJECT) as store:
        assert store.pending_sessions() == [("codex", "s1")]
        tracker = _SessionTracker()
        tracker.load("p", store)
        assert tracker.refresh("p", store, now=0) == (1, [])
        assert store.pending_sessions() == []
        assert store.connection.execute("SELECT rule_version FROM session_analysis").fetchall() == [
            (analysis.RULE_VERSION,)
        ]
        new = store.connection.execute("SELECT fingerprint FROM session_findings").fetchall()
        if count == 3:
            assert new and set(new).isdisjoint(old)


def test_reducer_release_refolds_all_providers_before_tracker_load(tmp_path, monkeypatch):
    with Store(tmp_path, PROJECT) as store:
        for provider in ("codex", "claude"):
            _repeat(store, "s1", provider=provider)
            store.refresh_session_findings(provider, "s1")
        expected = store.session_states()
        for value in expected.values():
            store._write_state(store.connection, replace(value, calls=900))

    monkeypatch.setattr(state, "VERSION", "wd-162.test-reducer")
    with Store(tmp_path, PROJECT) as store:
        tracker = _SessionTracker()
        tracker.load("p", store)
        for key, value in expected.items():
            assert tracker.session(*key) == value
        assert set(store.pending_sessions()) == {("codex", "s1"), ("claude", "s1")}
        stamps = store.connection.execute("SELECT updated_at FROM session_state").fetchall()
    with Store(tmp_path, PROJECT) as store:
        assert store.connection.execute("SELECT updated_at FROM session_state").fetchall() == stamps


def test_v8_migration_does_not_guess_analysis_version(tmp_path):
    with Store(tmp_path, PROJECT) as store:
        _repeat(store, "s1")
        store.refresh_session_findings("codex", "s1")
    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        db.execute("ALTER TABLE session_analysis DROP COLUMN rule_version")
        db.execute("PRAGMA user_version=8")
    with Store(tmp_path, PROJECT) as store:
        assert store.pending_sessions() == [("codex", "s1")]
        store.refresh_session_findings("codex", "s1")
        assert store.pending_sessions() == []


def test_failed_findings_refresh_keeps_the_session_pending(tmp_path, monkeypatch):
    with Store(tmp_path, PROJECT) as store:
        _repeat(store, "s1")
        store.refresh_session_findings("codex", "s1")
        old = store.connection.execute("SELECT * FROM session_analysis").fetchall()
        monkeypatch.setattr(analysis, "RULE_VERSION", "wd-010.test-release")
        store.connection.execute(
            "CREATE TRIGGER reject_findings BEFORE INSERT ON session_findings "
            "BEGIN SELECT RAISE(ABORT, 'test refresh failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="test refresh failure"):
            store.refresh_session_findings("codex", "s1")
        assert store.pending_sessions() == [("codex", "s1")]
        assert store.connection.execute("SELECT * FROM session_analysis").fetchall() == old


def test_failed_reducer_replay_preserves_old_state_and_marker(tmp_path, monkeypatch):
    with Store(tmp_path, PROJECT) as store:
        _repeat(store, "s1", provider="claude")
        before = store.session_states()
        marker = store.connection.execute("SELECT * FROM state_projection_versions").fetchall()
        store.connection.execute("UPDATE events SET envelope='bad'")
    monkeypatch.setattr(state, "VERSION", "wd-162.test-reducer")
    with pytest.raises(json.JSONDecodeError), Store(tmp_path, PROJECT):
        pass
    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        assert db.execute("SELECT * FROM state_projection_versions").fetchall() == marker
        assert (
            state.SessionState.from_json(
                db.execute("SELECT state_json FROM session_state").fetchone()[0]
            )
            == before["claude", "s1", ""]
        )
