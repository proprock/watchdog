"""Schema v7 annotation storage: progress state and per-finding verdicts."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.storage import StorageError, Store

PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _envelope(session_id: str, provider: str = "codex") -> Envelope:
    return Envelope(
        event_id=uuid.uuid4(),
        provider=provider,
        provider_version=None,
        surface="unknown",
        project_id=PROJECT,
        checkout_id=uuid.UUID("9f4edabb-c34e-5c24-a0da-24755024f12d"),
        session_id=session_id,
        agent_id=None,
        parent_agent_id=None,
        turn_id=None,
        native_event_id=None,
        kind="turn.start",
        occurred_at=None,
        received_at=datetime.now(UTC),
        source="hook",
        payload={provider: {"content": "omitted"}},
        availability={},
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "project", PROJECT) as opened:
        opened.put(_envelope("s1"))
        opened.put(_envelope("s2"))
        yield opened


def test_schema_is_version_7(store):
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 7
    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(session_labels)")}
    assert {"progress_state", "reviewer_note"} <= columns


def test_label_records_a_progress_state(store):
    label = store.label("codex", "s1", outcome="partial", task_type="debug", progress_state="stuck")

    assert label == {
        "task_outcome": "partial",
        "task_type": "debug",
        "progress_state": "stuck",
        "reviewer_note": None,
    }


def test_relabeling_preserves_a_field_the_caller_did_not_supply(store):
    store.label("codex", "s1", outcome="unknown", task_type=None, progress_state="slow")

    relabeled = store.label("codex", "s1", outcome="success", task_type="feature")

    assert relabeled["progress_state"] == "slow"
    assert relabeled["task_outcome"] == "success"


def test_an_unsupported_progress_state_is_rejected(store):
    with pytest.raises(StorageError, match="Invalid progress state"):
        store.label("codex", "s1", outcome="unknown", task_type=None, progress_state="wedged")


def test_a_session_verdict_is_recorded_and_updated(store):
    first = store.finding_verdict(
        "codex",
        "s1",
        rule="repeated_tool_outcome",
        rule_version="wd-010.v1",
        fingerprint="a" * 64,
        verdict="true_positive",
        note="same failing pytest command",
    )
    assert first["verdict"] == "true_positive"

    store.finding_verdict(
        "codex",
        "s1",
        rule="repeated_tool_outcome",
        rule_version="wd-010.v1",
        fingerprint="a" * 64,
        verdict="false_positive",
        note=None,
    )

    rows = store.connection.execute(
        "SELECT verdict, note FROM finding_verdicts WHERE session_id='s1'"
    ).fetchall()
    assert rows == [("false_positive", None)]


def test_an_unsupported_verdict_is_rejected(store):
    with pytest.raises(StorageError, match="Invalid finding verdict"):
        store.finding_verdict(
            "codex",
            "s1",
            rule="repeated_tool_outcome",
            rule_version="wd-010.v1",
            fingerprint="a" * 64,
            verdict="probably",
            note=None,
        )


def test_a_checkout_verdict_is_provider_independent(store):
    store.checkout_finding_verdict(
        "9f4edabb-c34e-5c24-a0da-24755024f12d",
        rule="diff_oscillation",
        rule_version="wd-010.v1",
        fingerprint="b" * 64,
        verdict="uncertain",
        note="concurrent editor",
    )

    rows = store.connection.execute(
        "SELECT checkout_id, verdict FROM checkout_finding_verdicts"
    ).fetchall()
    assert rows == [("9f4edabb-c34e-5c24-a0da-24755024f12d", "uncertain")]


def test_purging_a_session_drops_its_verdicts(store):
    store.label("codex", "s1", outcome="partial", task_type=None, progress_state="stuck")
    store.finding_verdict(
        "codex",
        "s1",
        rule="repeated_tool_outcome",
        rule_version="wd-010.v1",
        fingerprint="a" * 64,
        verdict="true_positive",
        note=None,
    )

    store.purge_provider_session("codex", "s1")

    assert store.connection.execute("SELECT COUNT(*) FROM finding_verdicts").fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM session_labels").fetchone()[0] == 0


def test_a_v6_database_upgrades_without_losing_labels(tmp_path):
    root = tmp_path / "legacy"
    with Store(root, PROJECT) as opened:
        opened.put(_envelope("s1"))
        opened.label("codex", "s1", outcome="partial", task_type="debug")
    database = root / "events.sqlite3"
    with sqlite3.connect(database) as raw:
        raw.execute("DROP TABLE IF EXISTS finding_verdicts")
        raw.execute("DROP TABLE IF EXISTS checkout_finding_verdicts")
        for column in ("progress_state", "reviewer_note"):
            raw.execute(f"ALTER TABLE session_labels DROP COLUMN {column}")
        raw.execute("PRAGMA user_version=6")

    with Store(root, PROJECT) as upgraded:
        assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert upgraded.connection.execute(
            "SELECT task_outcome, task_type, progress_state FROM session_labels"
        ).fetchall() == [("partial", "debug", None)]


def test_stored_events_remain_readable_after_the_upgrade(store):
    stored = store.connection.execute("SELECT envelope FROM events LIMIT 1").fetchone()[0]
    assert json.loads(stored)["project_id"] == str(PROJECT)
