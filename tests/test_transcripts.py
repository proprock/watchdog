import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich


def codex(event: Envelope) -> dict:
    payload = event.payload["codex"]
    assert isinstance(payload, dict)
    return cast(dict, payload)


def usage_payload(event: Envelope) -> dict:
    payload = codex(event)["usage"]
    assert isinstance(payload, dict)
    return cast(dict, payload)


def write_rollout(path: Path, session: str, records: list[dict]) -> None:
    rows = [
        {
            "type": "session_meta",
            "timestamp": "2026-09-07T10:00:00Z",
            "payload": {"id": session, "session_id": session, "cli_version": "0.153.4"},
        },
        *records,
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def usage(session: str, response: str, *, total: int, output: int, cached: int | None = 0) -> dict:
    counters = {
        "input_tokens": total - output,
        "output_tokens": output,
        "cached_input_tokens": cached,
        "total_tokens": total,
    }
    return {
        "type": "token_usage_record",
        "timestamp": "2026-09-07T10:00:01Z",
        "payload": {
            "session_id": session,
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "root_turn_id": "turn-1",
            "response_id": response,
            "usage": counters,
            "turn_token_usage": counters,
            "thread_token_usage": counters,
        },
    }


def hook(project, session: str, transcript: Path) -> Envelope:
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        kind="turn.end",
        source="hook",
        received_at=datetime(2026, 9, 7, tzinfo=UTC),
        payload={"codex": {"transcript_path": str(transcript)}},
    )


def test_enrichment_emits_deltas_without_cumulative_double_counting(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            usage(session, "response-2", total=16, output=6),
        ],
    )

    with Store(tmp_path / "data", project) as store:
        assert store.put(hook(project, session, transcript))
        assert enrich(store) == 2
        assert enrich(store) == 0
        events = [event for event in store.events() if event.kind == "usage"]

    assert [usage_payload(event)["delta"]["total_tokens"] for event in events] == [10, 6]
    assert [usage_payload(event)["cumulative"]["total_tokens"] for event in events] == [10, 16]
    assert all(
        event.availability[name] == "observed"
        for event in events
        for name in ("input_tokens", "output_tokens", "cached_input_tokens")
    )


def test_reader_keeps_partial_tail_and_marks_missing_cached_tokens_unavailable(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    first = usage(session, "response-1", total=10, output=4, cached=None)
    write_rollout(transcript, session, [])
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(first)[:30])

    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store) == 0
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(first)[30:] + "\n")
        assert enrich(store) == 1
        event = next(event for event in store.events() if event.kind == "usage")

    assert event.availability["cached_input_tokens"] == "unavailable"
    assert usage_payload(event)["cumulative"]["cached_input_tokens"] is None


def test_unsupported_format_records_one_gap_without_rejecting_the_hook(tmp_path):
    project = uuid4()
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text('{"type":"future_format"}\n', encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        assert store.put(hook(project, "session-1", transcript))
        assert enrich(store) == 0
        assert enrich(store) == 0
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert len(gaps) == 1
    assert gaps[0].availability["usage"] == "unavailable"


def test_session_mismatch_records_a_durable_gap(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    mismatched = usage("other-session", "response-1", total=10, output=4)
    write_rollout(transcript, session, [mismatched])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store) == 0
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert len(gaps) == 1 and codex(gaps[0])["reason"] == "usage_session_mismatch"


def test_counter_regression_creates_a_durable_gap(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(transcript, session, [usage(session, "response-1", total=10, output=4)])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store) == 1
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-2", total=5, output=2)) + "\n")
        assert enrich(store) == 1
        events = [event for event in store.events() if event.kind == "usage"]
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert usage_payload(events[-1])["delta"]["total_tokens"] is None
    assert len(gaps) == 1 and codex(gaps[0])["reason"] == "usage_counter_reset"


def test_unreadable_source_does_not_block_following_hook_or_source_retention(tmp_path):
    project = uuid4()
    session = "session-1"
    missing = tmp_path / "missing.jsonl"
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, missing))
        assert enrich(store) == 0
        # An enrichment failure is isolated from normal event admission.
        assert store.put(
            Envelope(
                provider="codex",
                project_id=project,
                session_id=session,
                kind="turn.end",
                source="hook",
            )
        )
        assert len(store.transcript_sources()) == 1
        store.maintain(now=datetime(2026, 10, 8, tzinfo=UTC))
        assert store.transcript_sources() == []


def test_truncation_revalidates_the_new_rollout_without_duplicate_usage(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(transcript, session, [usage(session, "response-1", total=10, output=4)])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store) == 1
        write_rollout(transcript, session, [])
        assert enrich(store) == 0
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-2", total=16, output=6)) + "\n")
        assert enrich(store) == 1
        events = [event for event in store.events() if event.kind == "usage"]

    assert [codex(event)["response_id"] for event in events] == ["response-1", "response-2"]
