"""WD-165: configuration follows native response time and conversation identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store

BASE = datetime(2026, 10, 10, tzinfo=UTC)


def event(project, kind, at, *, turn="t1", agent=None, provider="codex", **metadata):
    payload = {"metadata": metadata}
    if kind == "usage":
        payload["usage"] = {"delta": {"input_tokens": 100, "output_tokens": 10}}
    return Envelope(
        provider=provider,
        project_id=project,
        session_id="session",
        agent_id=agent,
        kind=kind,
        turn_id=turn,
        source="transcript",
        occurred_at=BASE + timedelta(seconds=at),
        received_at=BASE + timedelta(days=2, seconds=at),
        payload={provider: payload},
    )


def context(project, at, **kwargs):
    return event(
        project,
        "unknown",
        at,
        record_type="turn_context",
        attribution_projection="wd-165.v1",
        **kwargs,
    )


def values(store, usage):
    return store.connection.execute(
        "SELECT model, model_attribution, reasoning_effort, reasoning_effort_attribution "
        "FROM event_facts WHERE event_id=?",
        (str(usage.event_id),),
    ).fetchone()


def test_late_context_repairs_usage_without_rewriting_authority(tmp_path):
    project = uuid4()
    usage = event(project, "usage", 5)
    with Store(tmp_path, project) as store:
        store.put(event(project, "unknown", 20, model="future", reasoning_effort="low"))
        store.put(usage)
        raw = store.connection.execute(
            "SELECT envelope FROM events WHERE event_id=?", (str(usage.event_id),)
        ).fetchone()
        store.put(context(project, 1, model="gpt-test", reasoning_effort="high"))
        assert values(store, usage) == ("gpt-test", "inherited", "high", "inherited")
        assert (
            store.connection.execute(
                "SELECT envelope FROM events WHERE event_id=?", (str(usage.event_id),)
            ).fetchone()
            == raw
        )
        assert not store.put(usage)
    with Store(tmp_path, project) as store:
        assert values(store, usage) == ("gpt-test", "inherited", "high", "inherited")


def test_missing_effort_is_a_context_barrier_and_agents_providers_are_isolated(tmp_path):
    project = uuid4()
    first = event(project, "usage", 2, turn="t1")
    second = event(project, "usage", 4, turn="t2")
    child = event(project, "usage", 4, turn="t2", agent="child")
    with Store(tmp_path, project) as store:
        store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
        store.put(context(project, 3, turn="t2", model="gpt-b"))
        store.put(
            event(
                project,
                "unknown",
                3,
                provider="claude",
                turn="t2",
                model="claude-test",
                reasoning_effort="max",
            )
        )
        for usage in (first, second, child):
            store.put(usage)
        assert values(store, first) == ("gpt-a", "inherited", "high", "inherited")
        assert values(store, second) == ("gpt-b", "inherited", None, "unavailable")
        assert values(store, child) == (None, "unavailable", None, "unavailable")


def test_without_turn_native_time_selects_context_and_future_is_unknown(tmp_path):
    project = uuid4()
    before = event(project, "usage", 0, turn=None)
    between = event(project, "usage", 2, turn=None)
    after = event(project, "usage", 4, turn=None)
    with Store(tmp_path, project) as store:
        for usage in (before, between, after):
            store.put(usage)
        store.put(context(project, 3, turn="t2", model="gpt-b"))
        store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
        assert values(store, before) == (None, "unavailable", None, "unavailable")
        assert values(store, between) == ("gpt-a", "inherited", "high", "inherited")
        assert values(store, after) == ("gpt-b", "inherited", None, "unavailable")


def test_interrupted_reader_window_repairs_attribution_on_open(tmp_path):
    project = uuid4()
    usage = event(project, "usage", 5)
    with Store(tmp_path, project) as store:
        store.put(usage)
        with pytest.raises(RuntimeError, match="interrupted"):
            with store.defer_codex_state():
                store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
                assert values(store, usage)[0] is None
                raise RuntimeError("interrupted")
    with Store(tmp_path, project) as store:
        assert values(store, usage) == ("gpt-a", "inherited", "high", "inherited")
        assert store.connection.execute("SELECT * FROM codex_attribution_dirty").fetchall() == []


def test_failed_reprojection_keeps_atomic_repair_marker(tmp_path, monkeypatch):
    from agent_watchdog import codex_attribution

    project = uuid4()
    usage = event(project, "usage", 5)
    original = codex_attribution.refresh

    def fail(db, *args, **kwargs):
        original(db, *args, **kwargs)
        raise RuntimeError("failed refresh")

    with Store(tmp_path, project) as store:
        store.put(usage)
        with monkeypatch.context() as patch:
            patch.setattr(codex_attribution, "refresh", fail)
            with pytest.raises(RuntimeError, match="failed refresh"):
                with store.defer_codex_state():
                    store.put(context(project, 1, model="gpt-a"))
        assert values(store, usage)[0] is None
        assert store.connection.execute("SELECT * FROM codex_attribution_dirty").fetchall()
    with Store(tmp_path, project) as store:
        assert values(store, usage)[0] == "gpt-a"


def test_projection_upgrade_corrects_old_derived_values_and_preserves_receipts(tmp_path):
    project = uuid4()
    usage = event(project, "usage", 5)
    with Store(tmp_path, project) as store:
        store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
        store.put(usage)
        receipts = store.connection.execute("SELECT * FROM receipts ORDER BY event_id").fetchall()
        store.connection.execute("DELETE FROM fact_projection_versions")
        store.connection.execute(
            "UPDATE event_facts SET model='stale' WHERE event_id=?", (str(usage.event_id),)
        )
    with Store(tmp_path, project) as store:
        assert values(store, usage)[0] == "gpt-a"
        assert (
            store.connection.execute("SELECT * FROM receipts ORDER BY event_id").fetchall()
            == receipts
        )


def test_legacy_migration_keeps_colliding_provider_ids_separate(tmp_path):
    from test_storage_v6 import build_v5, claude_hook, claude_usage

    project = uuid4()
    usage = claude_usage(
        project,
        session="same",
        offset=3,
        request_id="r1",
        model="claude-x",
        response={"input_tokens": 10, "output_tokens": 1},
    )
    build_v5(
        tmp_path,
        project,
        [
            claude_hook(
                project,
                session="same",
                kind="turn.start",
                offset=1,
                prompt_id="turn",
                effort="high",
            ),
            event(project, "unknown", 2, reasoning_effort="low").model_copy(
                update={"session_id": "same"}
            ),
            usage,
        ],
    )
    with Store(tmp_path, project) as store:
        assert values(store, usage)[2:] == ("high", "inherited")


def test_conflicting_contexts_at_same_native_time_remain_unknown(tmp_path):
    project = uuid4()
    usage = event(project, "usage", 5)
    with Store(tmp_path, project) as store:
        store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
        store.put(context(project, 1, model="gpt-b", reasoning_effort="high"))
        store.put(usage)
        assert values(store, usage) == (None, "unavailable", "high", "inherited")


def test_late_context_does_not_restart_or_rewind_session_turn(tmp_path):
    project = uuid4()
    newer = event(project, "turn.start", 20, turn="new-turn")
    late = context(project, 1, turn="old-turn", model="gpt-a").model_copy(
        update={"received_at": newer.received_at + timedelta(seconds=1)}
    )
    with Store(tmp_path, project) as store:
        store.put(newer)
        before = store.session_states()[("codex", "session", "")]
        store.put(late)
        assert store.session_states()[("codex", "session", "")] == before


def test_usage_without_native_response_time_cannot_inherit_ingest_time_configuration(tmp_path):
    project = uuid4()
    usage = event(project, "usage", 5, turn=None).model_copy(update={"occurred_at": None})
    with Store(tmp_path, project) as store:
        store.put(context(project, 1, model="gpt-a", reasoning_effort="high"))
        store.put(context(project, 20, turn="t2", model="gpt-future", reasoning_effort="low"))
        store.put(usage)
        assert values(store, usage) == (None, "unavailable", None, "unavailable")
