"""Schema v6: queryable telemetry projections (WD-111).

A hand-built v5 database is migrated by reopening a real ``Store``. The
migration adds ``events.provider`` plus the ``event_facts`` projection table,
backfills both in insertion order, and leaves every stored envelope byte for
byte unchanged.
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store, canonical

V5_DDL = (
    "CREATE TABLE metadata (project_id TEXT NOT NULL)",
    "CREATE TABLE events (event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
    "session_id TEXT, kind TEXT NOT NULL, envelope TEXT NOT NULL)",
    "CREATE INDEX events_session ON events(session_id, received_at)",
    "CREATE TABLE artifacts (event_id TEXT NOT NULL REFERENCES events(event_id), "
    "name TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, "
    "PRIMARY KEY(event_id, name))",
    "CREATE TABLE pins (session_id TEXT PRIMARY KEY)",
    "CREATE TABLE receipts (event_id TEXT PRIMARY KEY REFERENCES events(event_id), "
    "digest TEXT NOT NULL)",
    "CREATE TABLE transcript_sources ("
    "provider TEXT NOT NULL, session_id TEXT NOT NULL, path TEXT NOT NULL, "
    "last_seen TEXT NOT NULL, reader TEXT, device INTEGER, inode INTEGER, "
    "size INTEGER, mtime INTEGER, offset INTEGER NOT NULL DEFAULT 0, "
    "tail BLOB NOT NULL DEFAULT X'', counters TEXT, last_error TEXT, "
    "error_signature TEXT, PRIMARY KEY(provider, session_id, path))",
    "CREATE TABLE diff_snapshots ("
    "snapshot_id TEXT PRIMARY KEY, checkout_id TEXT NOT NULL, "
    "fingerprint TEXT NOT NULL, byte_count INTEGER NOT NULL, observed_at TEXT NOT NULL)",
    "CREATE INDEX diff_snapshots_checkout ON diff_snapshots(checkout_id, observed_at)",
    "CREATE TABLE session_labels ("
    "provider TEXT NOT NULL, session_id TEXT NOT NULL, task_outcome TEXT NOT NULL, "
    "task_type TEXT, PRIMARY KEY(provider, session_id))",
)

BASE = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def build_v5(path, project_id, envelopes):
    """Write a v5 database whose ``events`` rows are the given envelopes."""
    db = sqlite3.connect(path / "events.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("BEGIN")
        for statement in V5_DDL:
            db.execute(statement)
        db.execute("INSERT INTO metadata VALUES (?)", (str(project_id),))
        for event in envelopes:
            document = canonical(event)
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    str(event.event_id),
                    event.received_at.isoformat(),
                    event.session_id,
                    event.kind,
                    document,
                ),
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, ?)",
                (str(event.event_id), hashlib.sha256(document.encode()).hexdigest()),
            )
        db.execute("PRAGMA user_version=5")
        db.execute("COMMIT")
    finally:
        db.close()


def claude_hook(project, *, session, kind, offset, prompt_id, effort=None, **metadata):
    md = {"hook_event_name": "PostToolUse", "session_id": session, "prompt_id": prompt_id}
    if effort is not None:
        md["effort"] = {"level": effort}
    md.update(metadata)
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={"claude": {"metadata": md, "content": "omitted"}},
        availability={},
    )


def claude_usage(project, *, session, offset, request_id, model, response):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        kind="usage",
        source="transcript",
        native_event_id=request_id,
        occurred_at=BASE + timedelta(seconds=offset),
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "claude": {
                "reader": "claude-transcript-v1",
                "model": model,
                "request_id": request_id,
                "usage": {"response": response},
            }
        },
        availability={},
    )


def claude_agent_event(
    project, *, session, kind, offset, agent_id, prompt_id, agent_type="Explore"
):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        agent_id=agent_id,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "claude": {
                "metadata": {
                    "hook_event_name": "SubagentStop" if kind == "agent.end" else "SubagentStart",
                    "session_id": session,
                    "prompt_id": prompt_id,
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                },
                "content": "omitted",
            }
        },
        availability={},
    )


def claude_subagent_usage(project, *, session, agent_id, offset, request_id, model, response):
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        agent_id=agent_id,
        kind="usage",
        source="transcript",
        native_event_id=request_id,
        occurred_at=BASE + timedelta(seconds=offset),
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "claude": {
                "reader": "claude-transcript-v1",
                "model": model,
                "request_id": request_id,
                "usage": {"response": response},
            }
        },
        availability={},
    )


def codex_hook(project, *, session, kind, offset, turn_id, model=None):
    md = {"hook_event_name": "PostToolUse", "session_id": session, "turn_id": turn_id}
    if model is not None:
        md["model"] = model
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        turn_id=turn_id,
        kind=kind,
        source="hook",
        received_at=BASE + timedelta(seconds=offset),
        payload={"codex": {"metadata": md, "content": "omitted"}},
        availability={},
    )


def codex_usage(project, *, session, offset, response_id, turn_id, delta):
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        turn_id=turn_id,
        kind="usage",
        source="transcript",
        native_event_id=response_id,
        occurred_at=BASE + timedelta(seconds=offset),
        received_at=BASE + timedelta(seconds=offset),
        payload={
            "codex": {
                "reader": "codex-rollout-v1",
                "response_id": response_id,
                "thread_id": session,
                "usage": {"cumulative": delta, "delta": delta},
            }
        },
        availability={},
    )


@pytest.fixture
def project():
    return uuid4()


def facts_by_event(store):
    columns = [row[1] for row in store.connection.execute("PRAGMA table_info(event_facts)")]
    rows = store.connection.execute("SELECT * FROM event_facts").fetchall()
    return {row[columns.index("event_id")]: dict(zip(columns, row, strict=True)) for row in rows}


def test_v5_to_v6_migration_shape_and_envelope_preservation(tmp_path, project):
    session = "11111111-1111-4111-8111-111111111111"
    events = [
        claude_hook(
            project,
            session=session,
            kind="tool.finish",
            offset=1,
            prompt_id="aaaa1111-0000-4000-8000-000000000001",
            effort="high",
            permission_mode="auto",
            tool_name="Bash",
            tool_use_id="tool-1",
            duration_ms=310,
        ),
        claude_usage(
            project,
            session=session,
            offset=2,
            request_id="req-1",
            model="claude-sonnet-5",
            response={
                "input_tokens": 2,
                "cache_read_input_tokens": 204155,
                "cache_creation_input_tokens": 1721,
                "output_tokens": 1294,
                "thinking_tokens": 436,
            },
        ),
    ]
    originals = {str(event.event_id): canonical(event) for event in events}
    build_v5(tmp_path, project, events)

    with Store(tmp_path, project) as store:
        db = store.connection
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6

        columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        assert "provider" in columns

        stored = dict(db.execute("SELECT event_id, envelope FROM events"))
        assert stored == originals
        assert dict(db.execute("SELECT event_id, provider FROM events")) == {
            key: "claude" for key in originals
        }

        facts = facts_by_event(store)
        assert set(facts) == set(originals)

        hook_fact = facts[str(events[0].event_id)]
        assert hook_fact["provider"] == "claude"
        assert hook_fact["kind"] == "tool.finish"
        assert hook_fact["session_id"] == session
        assert hook_fact["turn_id"] == "aaaa1111-0000-4000-8000-000000000001"
        assert hook_fact["turn_id_source"] == "prompt_id"
        assert hook_fact["conversation_id_source"] == "session_id"
        assert hook_fact["reasoning_effort"] == "high"
        assert hook_fact["reasoning_effort_attribution"] == "observed"
        assert hook_fact["model"] is None
        assert hook_fact["model_attribution"] == "unavailable"
        assert hook_fact["tool_name"] == "Bash"
        assert hook_fact["tool_use_id"] == "tool-1"
        assert hook_fact["tool_duration_ms"] == 310
        assert hook_fact["permission_mode"] == "auto"
        assert hook_fact["hook_event_name"] == "PostToolUse"
        assert hook_fact["received_at_us"] == int(events[0].received_at.timestamp() * 1_000_000)
        assert all(hook_fact[column] is None for column in ("input_tokens", "output_tokens"))

        usage_fact = facts[str(events[1].event_id)]
        assert usage_fact["kind"] == "usage"
        assert usage_fact["model"] == "claude-sonnet-5"
        assert usage_fact["model_attribution"] == "observed"
        assert usage_fact["input_tokens"] == 2
        assert usage_fact["cached_input_tokens"] == 204155
        assert usage_fact["cache_write_input_tokens"] == 1721
        assert usage_fact["output_tokens"] == 1294
        assert usage_fact["reasoning_output_tokens"] == 436
        assert usage_fact["total_tokens"] is None
        assert usage_fact["reasoning_effort"] == "high"
        assert usage_fact["reasoning_effort_attribution"] == "inherited"
        assert usage_fact["turn_id"] is None
        assert usage_fact["turn_id_source"] == "unavailable"


def test_v6_migration_is_idempotent_across_reopen(tmp_path, project):
    session = "22222222-2222-4222-8222-222222222222"
    events = [
        codex_hook(
            project,
            session=session,
            kind="tool.finish",
            offset=1,
            turn_id="t1",
            model="gpt-5.6",
        ),
        codex_usage(
            project,
            session=session,
            offset=2,
            response_id="resp-1",
            turn_id="t1",
            delta={
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "cache_write_input_tokens": 0,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
            },
        ),
    ]
    build_v5(tmp_path, project, events)

    with Store(tmp_path, project) as store:
        first = facts_by_event(store)
    with Store(tmp_path, project) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert facts_by_event(store) == first

    codex_usage_fact = first[str(events[1].event_id)]
    assert codex_usage_fact["model"] == "gpt-5.6"
    assert codex_usage_fact["model_attribution"] == "inherited"
    assert codex_usage_fact["total_tokens"] == 120
    assert codex_usage_fact["conversation_id_source"] == "thread_id"
    assert codex_usage_fact["turn_id"] == "t1"


def test_v6_put_populates_event_facts_for_new_events(tmp_path, project):
    build_v5(tmp_path, project, [])
    session = "33333333-3333-4333-8333-333333333333"
    with Store(tmp_path, project) as store:
        hook = codex_hook(
            project,
            session=session,
            kind="tool.finish",
            offset=1,
            turn_id="t9",
            model="gpt-5.6",
        )
        usage = codex_usage(
            project,
            session=session,
            offset=2,
            response_id="resp-9",
            turn_id="t9",
            delta={
                "input_tokens": 7,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
                "total_tokens": 10,
            },
        )
        assert store.put(hook)
        assert store.put(usage)
        facts = facts_by_event(store)
        assert facts[str(hook.event_id)]["model"] == "gpt-5.6"
        assert facts[str(hook.event_id)]["model_attribution"] == "observed"
        assert facts[str(usage.event_id)]["model_attribution"] == "inherited"
        assert facts[str(usage.event_id)]["input_tokens"] == 7


def test_v6_inheritance_never_crosses_conversations_or_providers(tmp_path, project):
    claude_session = "55555555-5555-4555-8555-555555555555"
    codex_session = "66666666-6666-4666-8666-666666666666"
    events = [
        # Claude turn observes effort=high; a later Codex usage in another
        # conversation must not inherit it, and Claude hooks have no model.
        claude_hook(
            project,
            session=claude_session,
            kind="turn.start",
            offset=1,
            prompt_id="pp-1",
            effort="high",
            permission_mode="plan",
        ),
        codex_hook(project, session=codex_session, kind="tool.finish", offset=2, turn_id="ct1"),
        codex_usage(
            project,
            session=codex_session,
            offset=3,
            response_id="r-x",
            turn_id="ct1",
            delta={
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
                "total_tokens": 2,
            },
        ),
        claude_usage(
            project,
            session=claude_session,
            offset=4,
            request_id="r-y",
            model="claude-opus-5",
            response={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    build_v5(tmp_path, project, events)
    with Store(tmp_path, project) as store:
        facts = facts_by_event(store)

    codex_usage_fact = facts[str(events[2].event_id)]
    assert codex_usage_fact["reasoning_effort"] is None
    assert codex_usage_fact["reasoning_effort_attribution"] == "unavailable"
    assert codex_usage_fact["model"] is None
    assert codex_usage_fact["model_attribution"] == "unavailable"

    claude_usage_fact = facts[str(events[3].event_id)]
    assert claude_usage_fact["model"] == "claude-opus-5"
    assert claude_usage_fact["model_attribution"] == "observed"
    assert claude_usage_fact["reasoning_effort"] == "high"
    assert claude_usage_fact["reasoning_effort_attribution"] == "inherited"


def test_v6_subagent_usage_is_correlated_to_the_parent_turn(tmp_path, project):
    session = "77777777-7777-4777-8777-777777777777"
    parent_turn = "pp-parent-1"
    events = [
        claude_hook(
            project,
            session=session,
            kind="turn.start",
            offset=0,
            prompt_id=parent_turn,
            effort="high",
        ),
        claude_agent_event(
            project,
            session=session,
            kind="agent.end",
            offset=5,
            agent_id="ag-1",
            prompt_id=parent_turn,
        ),
        claude_subagent_usage(
            project,
            session=session,
            agent_id="ag-1",
            offset=6,
            request_id="sub-req-1",
            model="claude-haiku-4-5",
            response={"input_tokens": 4, "output_tokens": 90},
        ),
        claude_usage(
            project,
            session=session,
            offset=7,
            request_id="parent-req-1",
            model="claude-opus-5",
            response={"input_tokens": 2, "output_tokens": 500},
        ),
    ]
    build_v5(tmp_path, project, events)
    with Store(tmp_path, project) as store:
        facts = facts_by_event(store)

    sub = facts[str(events[2].event_id)]
    assert sub["agent_id"] == "ag-1"
    assert sub["session_id"] == session
    assert sub["turn_id"] == parent_turn
    assert sub["turn_id_source"] == "parent_agent"
    # The subagent keeps its own observed model, not the parent's.
    assert sub["model"] == "claude-haiku-4-5"
    assert sub["model_attribution"] == "observed"

    parent = facts[str(events[3].event_id)]
    assert parent["turn_id"] is None  # parent usage rows still carry no turn
    assert parent["model"] == "claude-opus-5"
    # Parent model resolution must not be polluted by the subagent row.
    assert parent["model_attribution"] == "observed"


def test_v6_subagent_usage_without_a_matching_agent_event_stays_unattributed(tmp_path, project):
    session = "88888888-8888-4888-8888-888888888888"
    events = [
        claude_subagent_usage(
            project,
            session=session,
            agent_id="ghost",
            offset=1,
            request_id="r-ghost",
            model="claude-haiku-4-5",
            response={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    build_v5(tmp_path, project, events)
    with Store(tmp_path, project) as store:
        sub = facts_by_event(store)[str(events[0].event_id)]
    assert sub["turn_id"] is None
    assert sub["turn_id_source"] == "unavailable"


def test_v6_subagent_turn_correlation_does_not_cross_agents(tmp_path, project):
    session = "99999999-9999-4999-8999-999999999999"
    events = [
        claude_agent_event(
            project, session=session, kind="agent.end", offset=1, agent_id="a1", prompt_id="turn-A"
        ),
        claude_agent_event(
            project, session=session, kind="agent.end", offset=2, agent_id="a2", prompt_id="turn-B"
        ),
        claude_subagent_usage(
            project,
            session=session,
            agent_id="a2",
            offset=3,
            request_id="r2",
            model="claude-haiku-4-5",
            response={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    build_v5(tmp_path, project, events)
    with Store(tmp_path, project) as store:
        sub = facts_by_event(store)[str(events[2].event_id)]
    assert sub["turn_id"] == "turn-B"


def test_v6_put_correlates_a_new_subagent_usage_row(tmp_path, project):
    build_v5(tmp_path, project, [])
    session = "a0a0a0a0-a0a0-4a0a-8a0a-a0a0a0a0a0a0"
    with Store(tmp_path, project) as store:
        agent_end = claude_agent_event(
            project,
            session=session,
            kind="agent.end",
            offset=1,
            agent_id="live-1",
            prompt_id="live-turn",
        )
        usage = claude_subagent_usage(
            project,
            session=session,
            agent_id="live-1",
            offset=2,
            request_id="live-req",
            model="claude-haiku-4-5",
            response={"input_tokens": 1, "output_tokens": 2},
        )
        assert store.put(agent_end)
        assert store.put(usage)
        sub = facts_by_event(store)[str(usage.event_id)]
    assert sub["turn_id"] == "live-turn"
    assert sub["turn_id_source"] == "parent_agent"


def test_v6_delete_removes_the_matching_fact_row(tmp_path, project):
    session = "44444444-4444-4444-8444-444444444444"
    events = [
        codex_hook(
            project,
            session=session,
            kind="tool.finish",
            offset=1,
            turn_id="t1",
            model="gpt-5.6",
        ),
    ]
    build_v5(tmp_path, project, events)
    with Store(tmp_path, project) as store:
        assert len(facts_by_event(store)) == 1
        store.purge_provider_session("codex", session)
        assert facts_by_event(store) == {}
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_v6_migration_creates_the_five_analysis_indexes(tmp_path, project):
    build_v5(tmp_path, project, [])
    with Store(tmp_path, project) as store:
        indexes = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='event_facts' "
                "AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        }
    assert indexes == {
        "event_facts_context",
        "event_facts_usage_time",
        "event_facts_usage_dim",
        "event_facts_tool_pair",
        "event_facts_tool_cost",
    }
    # Query-plan regression that these indexes are actually chosen lives in
    # tests/test_facts_query.py (WD-112).
