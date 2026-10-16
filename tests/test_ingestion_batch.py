"""Durable, bounded inbox batches (WD-172)."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_state import KEY, PROJECT, edit, event, replay, terminal

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Inbox, QuotaExceeded, Store


def publish(root, count):
    project = uuid4()
    inbox = Inbox(root)
    events = [
        Envelope(
            provider="codex",
            project_id=project,
            session_id="batch",
            kind="tool.finish",
            source="hook",
        )
        for _ in range(count)
    ]
    for item in events:
        inbox.publish(item)
    return project, inbox, events


def test_bounded_batches_commit_before_ack_and_fold_once(tmp_path, monkeypatch):
    project, inbox, events = publish(tmp_path, 55)
    with Store(tmp_path, project) as store:
        statements = []
        store.connection.set_trace_callback(statements.append)
        replay = store._replay_codex_state
        folds = []

        def counted(db, key):
            folds.append(key)
            return replay(db, key)

        monkeypatch.setattr(store, "_replay_codex_state", counted)
        unlink = Path.unlink

        def acknowledged(path, *args, **kwargs):
            if path.parent == inbox.directory:
                assert not store.connection.in_transaction
                with closing(sqlite3.connect(tmp_path / "events.sqlite3")) as reader:
                    assert reader.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] > 0
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", acknowledged)
        result = inbox.drain(store, batch_size=10, time_budget=None)
        assert result.inserted == len(events)
        assert statements.count("COMMIT") == 6
        assert len(folds) == 6
        assert store.connection.execute("SELECT COUNT(*) FROM codex_state_dirty").fetchone() == (0,)


@pytest.mark.parametrize("phase,committed", [("before", 0), ("after", 20)])
def test_killed_batch_replays_all_events_once(tmp_path, phase, committed):
    project, inbox, events = publish(tmp_path, 20)
    probe = Path(__file__).with_name("storage_crash_probe.py")
    result = subprocess.run(
        [sys.executable, str(probe), str(tmp_path), str(project), phase],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 91, result.stderr
    assert len(list(inbox.directory.glob("*.json"))) == 20
    with Store(tmp_path, project) as store:
        assert len(store.events()) == committed
        result = inbox.drain(store)
        assert (result.inserted, result.duplicates) == (20 - committed, committed)
        assert {event.event_id for event in store.events()} == {event.event_id for event in events}
        assert store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone() == (20,)


def test_batch_failure_restores_state_cache_and_leaves_every_input(tmp_path):
    project, inbox, _ = publish(tmp_path, 3)
    with Store(tmp_path, project) as store:
        store.connection.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON events "
            "WHEN (SELECT COUNT(*) FROM events)=1 "
            "BEGIN SELECT RAISE(ABORT, 'second insert'); END"
        )
        with pytest.raises(sqlite3.DatabaseError):
            inbox.drain(store)
        assert store.events() == []
        assert store.touched == {}
        assert len(list(inbox.directory.glob("*.json"))) == 3
        store.connection.execute("DROP TRIGGER fail_second")
        assert inbox.drain(store).inserted == 3


def test_deadline_resumes_after_committed_chunk(tmp_path, monkeypatch):
    project, inbox, _ = publish(tmp_path, 12)
    clock = iter(range(100))
    monkeypatch.setattr("agent_watchdog.storage.time.monotonic", lambda: next(clock))
    with Store(tmp_path, project) as store:
        results = [inbox.drain(store, batch_size=5, time_budget=0.1) for _ in range(3)]
        assert [result.inserted for result in results] == [5, 5, 2]
        assert len(store.events()) == 12


def test_batch_quota_falls_back_to_individual_admission(tmp_path, monkeypatch):
    project, inbox, _ = publish(tmp_path, 3)
    with Store(tmp_path, project) as store:
        # Enough conservative headroom for one record, but not a whole batch.
        monkeypatch.setattr("agent_watchdog.storage.resources.available", lambda *args: 70000)
        assert inbox.drain(store).inserted == 3


def test_real_quota_failure_never_acknowledges_uncommitted_input(tmp_path, monkeypatch):
    project, inbox, _ = publish(tmp_path, 3)
    with Store(tmp_path, project) as store:
        monkeypatch.setattr("agent_watchdog.storage.resources.available", lambda *args: 0)
        monkeypatch.setattr(store, "_maintain", lambda *args: None)
        with pytest.raises(QuotaExceeded):
            inbox.drain(store)
        assert store.events() == []
        assert len(list(inbox.directory.glob("*.json"))) == 3


def test_batch_projection_matches_causal_replay_and_reopen(tmp_path):
    events = [
        edit(0),
        event(1, call="run", tool_input={"cmd": "pytest"}, kind="tool.start"),
        event(2, call="run", response="Process running with session ID 42"),
        event(3, "write_stdin", call="poll", tool_input={"session_id": 42}, response=terminal()),
        event(4, kind="turn.start", turn="next"),
        edit(5, turn="next"),
    ]
    inbox = Inbox(tmp_path)
    for item in events:
        inbox.publish(item)
    # A replay and a UUID conflict in the same transaction must not affect state.
    inbox.publish(events[0])
    inbox.publish(events[0].model_copy(update={"kind": "interrupt"}))
    with Store(tmp_path, PROJECT) as store:
        # Inbox filenames are random: establish the authoritative receipt before
        # testing replays and conflicts, rather than assuming directory order.
        store.put(events[0])
        result = inbox.drain(store)
        assert (result.inserted, result.duplicates, result.quarantined) == (5, 2, 1)
        assert store.session_states()[KEY] == replay(events)
    with Store(tmp_path, PROJECT) as store:
        assert store.session_states()[KEY] == replay(events)


def test_failed_batch_projection_rolls_back_events_and_cached_claude_state(tmp_path, monkeypatch):
    project, inbox, _ = publish(tmp_path, 3)
    claude = Envelope(
        provider="claude", project_id=project, session_id="claude", kind="turn.start", source="hook"
    )
    inbox.publish(claude)
    with Store(tmp_path, project) as store:

        def fail_replay(*args):
            raise RuntimeError("projection failed")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_replay_codex_state", fail_replay)
            with pytest.raises(RuntimeError, match="projection failed"):
                inbox.drain(store)
        assert store.events() == []
        assert store.touched == {}
        assert store.session_states() == {}
        assert len(list(inbox.directory.glob("*.json"))) == 4
        assert inbox.drain(store).inserted == 4
