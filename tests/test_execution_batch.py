"""Durable, bounded Codex execution-state deferral for reader windows."""

import pytest
from test_execution_state import KEY, PROJECT, edit, event, replay, terminal

from agent_watchdog.storage import Store


def dirty(store):
    return store.connection.execute(
        "SELECT session_id, agent_id FROM codex_state_dirty ORDER BY session_id, agent_id"
    ).fetchall()


def test_reader_window_replays_once_per_affected_identity(tmp_path, monkeypatch):
    events = [
        edit(0),
        event(1, call="one", tool_input={"cmd": "pytest"}, kind="tool.start"),
        event(2, call="one", response=terminal()),
        event(3, call="two", tool_input={"cmd": "pytest"}, response=terminal(), agent="child"),
        event(4, kind="turn.start", turn="next"),
        edit(5, turn="next"),
    ]
    with Store(tmp_path, PROJECT) as store:
        original = store._replay_codex_state
        calls = []

        def counted(db, key):
            calls.append(key)
            return original(db, key)

        monkeypatch.setattr(store, "_replay_codex_state", counted)
        with store.defer_codex_state():
            for item in events:
                store.put(item)
            assert calls == []
            assert dirty(store) == [("session", ""), ("session", "child")]
            assert store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 6
        assert sorted(calls) == [KEY, ("codex", "session", "child")]
        assert store.session_states()[KEY] == replay(events)
        assert store.touched[KEY] == replay(events)
        assert dirty(store) == []
        with store.defer_codex_state():
            store.put(event(6, call="three", tool_input={"cmd": "pytest"}, response=terminal()))
        assert calls.count(KEY) == 2


def test_interrupted_window_is_repaired_on_open_with_current_version(tmp_path):
    events = [edit(0), event(1, call="run", tool_input={"cmd": "pytest"}, response=terminal())]
    with Store(tmp_path, PROJECT) as store:
        with pytest.raises(RuntimeError, match="interrupted"):
            with store.defer_codex_state():
                for item in events:
                    store.put(item)
                raise RuntimeError("interrupted reader")
        assert dirty(store) == [("session", "")]
        assert store.session_states() == {}
    with Store(tmp_path, PROJECT) as reopened:
        assert reopened.session_states()[KEY] == replay(events)
        assert reopened.touched[KEY] == replay(events)
        assert dirty(reopened) == []


def test_failed_flush_rolls_back_state_and_preserves_recovery_marker(tmp_path, monkeypatch):
    first, second = edit(0), edit(1)
    with Store(tmp_path, PROJECT) as store:
        store.put(first)
        before = store.session_states()[KEY]
        original = store._write_state

        def failed(db, state):
            original(db, state)
            raise RuntimeError("failed replay")

        monkeypatch.setattr(store, "_write_state", failed)
        with pytest.raises(RuntimeError, match="failed replay"):
            with store.defer_codex_state():
                store.put(second)
        assert dirty(store) == [("session", "")]
        assert store.session_states()[KEY] == before
        assert store.touched[KEY] == before
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    with Store(tmp_path, PROJECT) as reopened:
        assert reopened.session_states()[KEY] == replay([first, second])
        assert dirty(reopened) == []


def test_duplicate_receipt_does_not_create_dirty_work(tmp_path, monkeypatch):
    item = edit(0)
    with Store(tmp_path, PROJECT) as store:
        store.put(item)

        def unexpected(*args):
            pytest.fail("duplicate receipt caused a replay")

        monkeypatch.setattr(store, "_replay_codex_state", unexpected)
        with store.defer_codex_state():
            assert store.put(item) is False
            assert dirty(store) == []
        assert store.session_states()[KEY].calls == 1


def test_failed_raw_commit_cannot_leave_a_dirty_ghost(tmp_path, monkeypatch):
    with Store(tmp_path, PROJECT) as store:
        original = store._apply_state

        def failed(db, envelope):
            original(db, envelope)
            raise RuntimeError("failed raw transaction")

        monkeypatch.setattr(store, "_apply_state", failed)
        with pytest.raises(RuntimeError, match="failed raw transaction"):
            with store.defer_codex_state():
                store.put(edit(0))
        assert dirty(store) == []
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert store.touched == {}


def test_claude_state_remains_immediate_during_codex_window(tmp_path):
    item = edit(0).model_copy(
        update={"provider": "claude", "payload": {"claude": {"tool_name": "Edit"}}}
    )
    with Store(tmp_path, PROJECT) as store:
        with store.defer_codex_state():
            store.put(item)
            assert store.session_states()[("claude", "session", "")].calls == 1
            assert dirty(store) == []


def test_purged_session_does_not_leave_dirty_keys_or_reappear(tmp_path):
    with Store(tmp_path, PROJECT) as store:
        with store.defer_codex_state():
            store.put(edit(0))
            store.purge_provider_session("codex", "session")
            assert dirty(store) == []
        assert store.session_states() == {}
