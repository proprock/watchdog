"""Behavioural tests for the versioned Claude transcript reader (WD-022b)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import FAILURE_CODES, enrich

SESSION = "11111111-1111-4111-8111-111111111111"


def claude(event: Envelope) -> dict:
    payload = event.payload["claude"]
    assert isinstance(payload, dict)
    return cast(dict, payload)


def response(event: Envelope) -> dict:
    usage = claude(event)["usage"]
    assert isinstance(usage, dict)
    return cast(dict, usage["response"])


def assistant(
    session: str,
    request: str,
    *,
    model: str | None = "claude-sonnet-5",
    block: int = 0,
    input_tokens: int = 2,
    cache_read: int = 0,
    cache_creation: int = 0,
    output_tokens: int = 10,
    thinking: int = 0,
    timestamp: str = "2026-09-09T04:58:34.955Z",
    version: str = "2.1.260",
    sidechain: bool = False,
    usage: dict | None = None,
) -> dict:
    message: dict = {
        "id": request.replace("req_", "msg_"),
        "role": "assistant",
        "stop_reason": "end_turn",
        "usage": usage
        if usage is not None
        else {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "output_tokens": output_tokens,
            "output_tokens_details": {"thinking_tokens": thinking},
        },
    }
    if model is not None:
        message["model"] = model
    line: dict = {
        "type": "assistant",
        "sessionId": session,
        "requestId": request,
        "apiBlockIndex": block,
        "timestamp": timestamp,
        "version": version,
        "uuid": f"{request}-{block}",
        "message": message,
    }
    if sidechain:
        line["isSidechain"] = True
    return line


def write_transcript(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def hook(project, session: str, transcript: Path) -> Envelope:
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        kind="turn.end",
        source="hook",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        payload={"claude": {"transcript_path": str(transcript)}},
    )


def subagent_hook(project, session: str, agent_id: str, agent_transcript: Path) -> Envelope:
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=session,
        agent_id=agent_id,
        kind="agent.end",
        source="hook",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        payload={"claude": {"agent_transcript_path": str(agent_transcript)}},
    )


def usage_events(store: Store) -> list[Envelope]:
    return [event for event in store.events() if event.kind == "usage"]


def gap_events(store: Store) -> list[Envelope]:
    return [event for event in store.events() if event.kind == "observation.gap"]


def test_one_response_becomes_one_usage_event(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        [
            assistant(
                SESSION,
                "req_A",
                input_tokens=5,
                cache_read=7,
                cache_creation=9,
                output_tokens=11,
                thinking=3,
            )
        ],
    )
    with Store(tmp_path / "data", project) as store:
        assert store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 1
        assert enrich(store).accepted == 0
        events = usage_events(store)

    assert len(events) == 1
    event = events[0]
    assert response(event) == {
        "input_tokens": 5,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 9,
        "output_tokens": 11,
        "thinking_tokens": 3,
    }
    assert "total_tokens" not in response(event)
    assert claude(event)["model"] == "claude-sonnet-5"
    assert claude(event)["request_id"] == "req_A"
    assert claude(event)["cc_version"] == "2.1.260"
    assert event.native_event_id == "req_A"
    assert event.turn_id is None
    assert event.session_id == SESSION
    assert event.agent_id is None
    assert event.occurred_at == datetime(2026, 9, 9, 4, 58, 34, 955000, tzinfo=UTC)
    for name in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
        assert event.availability[name] == "observed"
    assert event.availability["model"] == "observed"


def test_repeated_request_blocks_do_not_double_count(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        [
            assistant(SESSION, "req_A", block=0, output_tokens=10),
            assistant(SESSION, "req_A", block=1, output_tokens=10),
            assistant(SESSION, "req_A", block=2, output_tokens=10),
            assistant(SESSION, "req_B", block=0, output_tokens=4),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 2
        assert enrich(store).accepted == 0
        events = usage_events(store)

    assert sorted(claude(event)["request_id"] for event in events) == ["req_A", "req_B"]
    assert [
        response(event)["output_tokens"]
        for event in events
        if claude(event)["request_id"] == "req_A"
    ] == [10]


def test_repeated_block_in_a_later_read_window_adds_no_row(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, [assistant(SESSION, "req_A", block=0)])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 1
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(assistant(SESSION, "req_A", block=1)) + "\n")
        assert enrich(store).accepted == 0
        assert len(usage_events(store)) == 1


def test_sidechain_lines_in_the_main_transcript_are_skipped(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        [
            assistant(SESSION, "req_main", output_tokens=8),
            assistant(SESSION, "req_child", output_tokens=99, sidechain=True),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 1
        events = usage_events(store)

    assert [claude(event)["request_id"] for event in events] == ["req_main"]


def test_missing_cache_counter_is_marked_unavailable(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        [
            assistant(
                SESSION,
                "req_A",
                usage={
                    "input_tokens": 4,
                    "output_tokens": 6,
                    "output_tokens_details": {"thinking_tokens": 1},
                },
            )
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 1
        event = usage_events(store)[0]

    assert response(event)["cache_read_input_tokens"] is None
    assert event.availability["cache_read_input_tokens"] == "unavailable"
    assert event.availability["input_tokens"] == "observed"


def test_partial_trailing_line_is_completed_on_the_next_pass(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    first = json.dumps(assistant(SESSION, "req_A"))
    transcript.write_text(first[:40], encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 0
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(first[40:] + "\n")
        assert enrich(store).accepted == 1
        assert len(usage_events(store)) == 1


def test_truncation_reprocesses_without_duplicate_usage(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, [assistant(SESSION, "req_A")])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 1
        write_transcript(transcript, [assistant(SESSION, "req_A"), assistant(SESSION, "req_B")])
        assert enrich(store).accepted == 1
        assert sorted(claude(event)["request_id"] for event in usage_events(store)) == [
            "req_A",
            "req_B",
        ]


def test_wrong_session_id_records_a_durable_gap(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, [assistant("22222222-2222-4222-8222-222222222222", "req_A")])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        assert enrich(store).accepted == 0
        assert enrich(store).accepted == 0
        gaps = gap_events(store)

    assert len(gaps) == 1
    assert claude(gaps[0])["reason"] == "claude_session_mismatch"
    assert gaps[0].availability["usage"] == "unavailable"


def test_bad_usage_value_records_a_durable_gap(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        [assistant(SESSION, "req_A", usage={"input_tokens": -3, "output_tokens": 6})],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        first = enrich(store)
        assert first.accepted == 0
        assert set(first.active_failures) <= FAILURE_CODES
        gaps = gap_events(store)

    assert len(gaps) == 1
    assert claude(gaps[0])["reason"] == "claude_usage_invalid"


def test_unreadable_transcript_isolates_and_is_retired_by_retention(tmp_path):
    project = uuid4()
    missing = tmp_path / "missing.jsonl"
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, missing))
        assert enrich(store).accepted == 0
        assert store.put(
            Envelope(
                provider="claude",
                project_id=project,
                session_id=SESSION,
                kind="turn.end",
                source="hook",
            )
        )
        assert len(store.transcript_sources()) == 1
        store.maintain(now=datetime(2026, 10, 10, tzinfo=UTC))
        assert store.transcript_sources() == []


def test_subagent_transcript_is_correlated_to_the_parent(tmp_path):
    project = uuid4()
    agent_transcript = tmp_path / "agent.jsonl"
    child_session = "33333333-3333-4333-8333-333333333333"
    write_transcript(
        agent_transcript,
        [assistant(child_session, "req_child", model="claude-haiku-4-5", output_tokens=42)],
    )
    with Store(tmp_path / "data", project) as store:
        assert store.put(subagent_hook(project, SESSION, "agent-7", agent_transcript))
        assert enrich(store).accepted == 1
        event = usage_events(store)[0]

    assert event.session_id == SESSION
    assert event.agent_id == "agent-7"
    assert claude(event)["model"] == "claude-haiku-4-5"
    assert response(event)["output_tokens"] == 42


def test_enrich_never_writes_hook_output(tmp_path):
    project = uuid4()
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, [assistant(SESSION, "req_A")])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, SESSION, transcript))
        result = enrich(store)

    assert result.accepted == 1
    assert result.failures == ()
