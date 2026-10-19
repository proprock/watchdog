import json
from uuid import uuid4

import pytest
from test_transcripts import CHILD, PARENT, codex, hook, model_hook, usage, write_rollout

import agent_watchdog.transcripts as transcripts
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich


def context(turn: str, model=None, effort=None, *, second=0):
    return {
        "type": "turn_context",
        "timestamp": f"2026-09-07T10:00:{second:02d}Z",
        "payload": {
            "turn_id": turn,
            "model": model,
            "effort": effort,
            "cwd": "PRIVATE-CONTEXT",
            "developer_instructions": "PRIVATE-CONTEXT",
        },
    }


def observations(store):
    return [
        event
        for event in store.events()
        if codex(event).get("metadata", {}).get("record_type") == "turn_context"
    ]


def test_context_switches_are_bounded_observations_without_lifecycle_changes(tmp_path):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        PARENT,
        [
            context("first", "gpt-5.5", "high"),
            context("second", "gpt-5.4-mini", "low", second=2),
            context("third", second=4),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, PARENT, path))
        result = enrich(store)
        events = observations(store)

    assert result.failures == ()
    assert len(events) == 3
    assert [event.kind for event in events] == ["unknown"] * 3
    assert [event.turn_id for event in events] == ["first", "second", "third"]
    assert [codex(event)["metadata"] for event in events] == [
        {
            "record_type": "turn_context",
            "model": model,
            "reasoning_effort": effort,
            "attribution_projection": "wd-165.v1",
        }
        for model, effort in [("gpt-5.5", "high"), ("gpt-5.4-mini", "low"), (None, None)]
    ]
    assert all(event.occurred_at is not None for event in events)
    assert "PRIVATE-CONTEXT" not in json.dumps([event.model_dump(mode="json") for event in events])


@pytest.mark.parametrize(
    "version", ["0.153.4", "0.160.0", "0.162.0-alpha.17.2", "0.163.0-alpha.10.2", "9.9.9"]
)
def test_context_grammar_is_version_gated(tmp_path, version):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, PARENT, [context("first", "gpt-5.5", "high")])
    path.write_text(path.read_text().replace("0.153.4", version), encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, PARENT, path))
        enrich(store)
        events = observations(store)
    assert len(events) == (0 if version == "9.9.9" else 1)
    assert all(event.provider_version == version for event in events)


def test_context_and_usage_survive_windows_restart_and_prior_reader_backfill(tmp_path, monkeypatch):
    project = uuid4()
    path = tmp_path / "rollout.jsonl"
    rows = [context("turn-1", "gpt-5.5", "high"), usage(PARENT, "response", total=10, output=3)]
    write_rollout(path, PARENT, rows)
    monkeypatch.setattr(transcripts, "READ_BYTES", 120)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, PARENT, path))
        for _ in range(12):
            enrich(store)
        context_ids = [event.event_id for event in observations(store)]
        usage_ids = [event.event_id for event in store.events() if event.kind == "usage"]
        assert len(context_ids) == len(usage_ids) == 1
        store.connection.execute(
            "UPDATE transcript_sources SET reader=?",
            (f"{transcripts.READER}|{transcripts.TOOL_READER}|0.153.4",),
        )
        store.connection.commit()
    with Store(tmp_path / "data", project) as store:
        for _ in range(12):
            enrich(store)
        assert [event.event_id for event in observations(store)] == context_ids
        assert [event.event_id for event in store.events() if event.kind == "usage"] == usage_ids
        reader = store.transcript_sources()[0]["reader"]
        assert isinstance(reader, str) and "codex-attribution-v1" in reader


def test_prior_reader_backfills_context_and_preserves_child_usage_identity(tmp_path):
    project = uuid4()
    path = tmp_path / f"rollout-{CHILD}.jsonl"
    write_rollout(path, PARENT, [usage(PARENT, "response", total=10, output=3)], thread=CHILD)
    with Store(tmp_path / "data", project) as store:
        store.put(model_hook(project, path, "gpt-5.4-mini", agent_id=CHILD))
        enrich(store)
        usage_id = next(event.event_id for event in store.events() if event.kind == "usage")
        # Simulate an existing cursor that read a turn_context with the old grammar.
        write_rollout(
            path,
            PARENT,
            [
                context("turn-1", "gpt-5.4-mini", "low"),
                usage(PARENT, "response", total=10, output=3),
            ],
            thread=CHILD,
        )
        stat = path.stat()
        store.connection.execute(
            "UPDATE transcript_sources SET reader=?, offset=?, size=?, mtime=?",
            (
                f"{transcripts.READER}|{transcripts.TOOL_READER}|0.153.4",
                stat.st_size,
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
        store.connection.commit()
        enrich(store)
        events = observations(store)
        assert len(events) == 1
        assert events[0].session_id == PARENT
        assert events[0].agent_id == CHILD
        assert [event.event_id for event in store.events() if event.kind == "usage"] == [usage_id]
        assert enrich(store).accepted == 0
