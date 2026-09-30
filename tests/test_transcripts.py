import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import agent_watchdog.transcripts as transcripts
from agent_watchdog.events import Envelope
from agent_watchdog.storage import StorageError, Store
from agent_watchdog.transcripts import FAILURE_CODES, enrich, failure_code


def codex(event: Envelope) -> dict:
    payload = event.payload["codex"]
    assert isinstance(payload, dict)
    return cast(dict, payload)


def usage_payload(event: Envelope) -> dict:
    payload = codex(event)["usage"]
    assert isinstance(payload, dict)
    return cast(dict, payload)


def write_rollout(
    path: Path, session: str, records: list[dict], *, thread: str | None = None
) -> None:
    """Write a rollout; ``thread`` makes it a subagent thread spawned by ``session``."""
    meta: dict = {"id": thread or session, "session_id": session, "cli_version": "0.153.4"}
    if thread is not None:
        meta["parent_thread_id"] = session
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": session, "depth": 1}}}
    rows = [
        {"type": "session_meta", "timestamp": "2026-09-07T10:00:00Z", "payload": meta},
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


def hook(
    project,
    session: str,
    transcript: Path,
    *,
    received_at: datetime = datetime(2026, 9, 7, tzinfo=UTC),
) -> Envelope:
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        kind="turn.end",
        source="hook",
        received_at=received_at,
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
        assert enrich(store).accepted == 2
        assert enrich(store).accepted == 0
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
        assert enrich(store).accepted == 0
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(first)[30:] + "\n")
        assert enrich(store).accepted == 1
        event = next(event for event in store.events() if event.kind == "usage")

    assert event.availability["cached_input_tokens"] == "unavailable"
    assert usage_payload(event)["cumulative"]["cached_input_tokens"] is None


def test_unsupported_format_records_one_gap_without_rejecting_the_hook(tmp_path):
    project = uuid4()
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text('{"type":"future_format"}\n', encoding="utf-8")
    with Store(tmp_path / "data", project) as store:
        assert store.put(hook(project, "session-1", transcript))
        assert enrich(store).accepted == 0
        assert enrich(store).accepted == 0
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
        assert enrich(store).accepted == 0
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert len(gaps) == 1 and codex(gaps[0])["reason"] == "usage_session_mismatch"


def test_counter_regression_creates_a_durable_gap(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(transcript, session, [usage(session, "response-1", total=10, output=4)])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store).accepted == 1
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-2", total=5, output=2)) + "\n")
        result = enrich(store)
        assert result.accepted == 1
        events = [event for event in store.events() if event.kind == "usage"]
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert usage_payload(events[-1])["delta"]["total_tokens"] is None
    assert len(gaps) == 1 and codex(gaps[0])["reason"] == "usage_counter_reset"
    assert result.active_failures == ("usage_counter_reset",)


def test_unreadable_source_does_not_block_following_hook_or_source_retention(tmp_path):
    project = uuid4()
    session = "session-1"
    missing = tmp_path / "missing.jsonl"
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, missing))
        assert enrich(store).accepted == 0
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
        assert enrich(store).accepted == 1
        write_rollout(transcript, session, [])
        assert enrich(store).accepted == 0
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-2", total=16, output=6)) + "\n")
        assert enrich(store).accepted == 1
        events = [event for event in store.events() if event.kind == "usage"]

    assert [codex(event)["response_id"] for event in events] == ["response-1", "response-2"]


def test_one_rollout_under_two_path_spellings_stores_each_response_once(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    (tmp_path / "sub").mkdir()
    twin = tmp_path / "sub" / ".." / "rollout.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            usage(session, "response-2", total=16, output=6),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store).accepted == 2
        store.put(hook(project, session, twin))
        assert len(store.transcript_sources()) == 2
        reread = enrich(store)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-3", total=25, output=9)) + "\n")
        grown = enrich(store)
        ids = [event.native_event_id for event in store.events() if event.kind == "usage"]

    assert (reread.accepted, reread.inert, reread.failures) == (0, False, ())
    assert (grown.accepted, grown.inert, grown.failures) == (1, False, ())
    assert ids == ["response-1", "response-2", "response-3"]


def test_response_stored_under_an_earlier_event_id_is_not_stored_again(tmp_path):
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
    earlier = Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        native_event_id="response-1",
        kind="usage",
        source="transcript",
        payload={"codex": {"reader": "codex-rollout-v1", "response_id": "response-1"}},
    )
    with Store(tmp_path / "data", project) as store:
        store.put(earlier)
        store.put(hook(project, session, transcript))
        result = enrich(store)
        events = [event for event in store.events() if event.kind == "usage"]

    assert (result.accepted, result.inert) == (1, False)
    assert [event.native_event_id for event in events] == ["response-1", "response-2"]
    # The skipped response still advances the cumulative baseline.
    assert usage_payload(events[1])["delta"]["total_tokens"] == 6


def test_reread_after_a_transient_read_failure_neither_duplicates_nor_rejects(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    moved = tmp_path / "moved.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            usage(session, "response-2", total=16, output=6),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        assert enrich(store).accepted == 2
        transcript.rename(moved)
        assert enrich(store).failures == ("transcript_unreadable",)
        moved.rename(transcript)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(session, "response-3", total=25, output=9)) + "\n")
        result = enrich(store)
        events = [event for event in store.events() if event.kind == "usage"]
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert (result.accepted, result.failures) == (1, ())
    assert [event.native_event_id for event in events] == ["response-1", "response-2", "response-3"]
    assert usage_payload(events[-1])["delta"]["total_tokens"] == 9
    assert [codex(gap)["reason"] for gap in gaps] == ["transcript_unreadable"]


def long_line(size: int) -> dict:
    # A real rollout's longest lines are item_completed event_msg records of
    # up to 6.7 MiB; they carry no usage but precede usage that does.
    return {"type": "event_msg", "payload": {"type": "item_completed", "text": "x" * size}}


def drain(store: Store) -> list:
    return [enrich(store) for _ in range(4)]


def test_a_line_longer_than_the_read_window_is_read_whole(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            long_line(5 * 1024**2 // 2),
            usage(session, "response-2", total=16, output=6),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        results = drain(store)
        events = store.events()

    assert sum(result.accepted for result in results) == 2
    assert [result.failures for result in results] == [()] * 4
    assert [event.native_event_id for event in events if event.kind == "usage"] == [
        "response-1",
        "response-2",
    ]
    assert not [event for event in events if event.kind == "observation.gap"]


def test_a_line_over_the_line_bound_is_skipped_with_a_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_LINE_BYTES", 3 * 1024**2 // 2)
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            long_line(5 * 1024**2 // 2),
            usage(session, "response-2", total=16, output=6),
        ],
    )
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        results = drain(store)
        events = store.events()

    assert sum(result.accepted for result in results) == 2
    assert [code for result in results for code in result.failures] == ["transcript_line_oversized"]
    assert [event.native_event_id for event in events if event.kind == "usage"] == [
        "response-1",
        "response-2",
    ]
    gaps = [event for event in events if event.kind == "observation.gap"]
    assert [codex(gap)["reason"] for gap in gaps] == ["transcript_line_oversized"]


def test_a_long_line_still_being_written_is_finished_on_a_later_pass(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(transcript, session, [usage(session, "response-1", total=10, output=4)])
    line = json.dumps(long_line(3 * 1024**2 // 2)) + "\n"
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(line[: 5 * 1024**2 // 4])
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        partial = drain(store)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(line[5 * 1024**2 // 4 :])
            stream.write(json.dumps(usage(session, "response-2", total=16, output=6)) + "\n")
        finished = drain(store)
        usage_ids = [event.native_event_id for event in store.events() if event.kind == "usage"]

    assert [result.accepted for result in partial] == [1, 0, 0, 0]
    assert sum(result.accepted for result in finished) == 1
    assert [result.failures for result in partial + finished] == [()] * 8
    assert usage_ids == ["response-1", "response-2"]


def test_a_source_stuck_on_a_long_line_is_read_again(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(
        transcript,
        session,
        [
            usage(session, "response-1", total=10, output=4),
            long_line(5 * 1024**2 // 2),
            usage(session, "response-2", total=16, output=6),
        ],
    )
    stat = transcript.stat()
    signature = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
    stored = Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        native_event_id="response-1",
        kind="usage",
        source="transcript",
        payload={"codex": {"reader": "codex-rollout-v1", "response_id": "response-1"}},
    )
    with Store(tmp_path / "data", project) as store:
        store.put(stored)
        store.put(hook(project, session, transcript))
        # The state the 1 MiB line limit left behind: offset 0, no tail, and an
        # error signature equal to the unchanged file's, so it was never retried.
        store.update_transcript_source(
            store.transcript_sources()[0],
            reader=None,
            device=stat.st_dev,
            inode=stat.st_ino,
            size=stat.st_size,
            mtime=stat.st_mtime_ns,
            offset=0,
            tail=b"",
            counters={"total_tokens": 10},
            last_error="rollout_line_invalid",
            error_signature=signature,
        )
        results = drain(store)
        usage_ids = [event.native_event_id for event in store.events() if event.kind == "usage"]

    assert sum(result.accepted for result in results) == 1
    assert [result.failures for result in results] == [()] * 4
    assert usage_ids == ["response-1", "response-2"]


PARENT = "01a0824c-df30-74d2-b88a-47f2a22f2229"
CHILD = "01a08265-0267-7171-899e-ea1113bd7459"


def model_hook(project, transcript: Path, model: str, agent_id: str | None = None) -> Envelope:
    # A Codex hook fired inside a subagent reports the parent session id, the
    # subagent's agent_id, and the subagent's own rollout as transcript_path.
    metadata = {"model": model, "turn_id": "turn-1"}
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=PARENT,
        agent_id=agent_id,
        turn_id="turn-1",
        kind="tool.start",
        source="hook",
        payload={"codex": {"transcript_path": str(transcript), "metadata": metadata}},
    )


def rollouts(tmp_path: Path, *, child_meta: str = CHILD) -> tuple[Path, Path]:
    parent = tmp_path / f"rollout-2026-09-08T20-34-31-{PARENT}.jsonl"
    child = tmp_path / f"rollout-2026-09-08T21-00-53-{CHILD}.jsonl"
    write_rollout(parent, PARENT, [usage(PARENT, "resp-parent", total=10, output=4)])
    write_rollout(
        child, PARENT, [usage(PARENT, "resp-child", total=7, output=3)], thread=child_meta
    )
    return parent, child


def test_a_subagent_rollout_under_the_parent_session_is_read_as_that_agent(tmp_path):
    project = uuid4()
    parent, child = rollouts(tmp_path)
    with Store(tmp_path / "data", project) as store:
        store.put(model_hook(project, parent, "gpt-5.5"))
        store.put(model_hook(project, child, "gpt-5.4-mini", agent_id=CHILD))
        result = enrich(store)
        events = store.events()
        facts = store.connection.execute(
            "SELECT native_event_id, session_id, agent_id, model, model_attribution "
            "FROM event_facts WHERE kind='usage' ORDER BY native_event_id"
        ).fetchall()

    assert (result.accepted, result.failures) == (2, ())
    assert not [event for event in events if event.kind == "observation.gap"]
    # The subagent's usage inherits its own model, not the parent's.
    assert facts == [
        ("resp-child", PARENT, CHILD, "gpt-5.4-mini", "inherited"),
        ("resp-parent", PARENT, None, "gpt-5.5", "inherited"),
    ]


def test_a_subagent_rollout_naming_another_thread_is_a_gap(tmp_path):
    project = uuid4()
    _, child = rollouts(tmp_path, child_meta="01a08265-2c75-71e3-b09e-bc02083224d4")
    with Store(tmp_path / "data", project) as store:
        store.put(model_hook(project, child, "gpt-5.4-mini", agent_id=CHILD))
        result = enrich(store)
        events = store.events()

    assert (result.accepted, result.failures) == (0, ("subagent_meta_mismatch",))
    assert not [event for event in events if event.kind == "usage"]


def test_a_subagent_rollout_the_former_reader_rejected_is_read_again(tmp_path):
    project = uuid4()
    _, child = rollouts(tmp_path)
    stat = child.stat()
    signature = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
    with Store(tmp_path / "data", project) as store:
        store.put(model_hook(project, child, "gpt-5.4-mini", agent_id=CHILD))
        # The state the former reader left: a session_meta_mismatch error whose
        # signature equals the unchanged file's, so it was never read again.
        store.update_transcript_source(
            store.transcript_sources()[0],
            reader=None,
            device=stat.st_dev,
            inode=stat.st_ino,
            size=stat.st_size,
            mtime=stat.st_mtime_ns,
            offset=stat.st_size,
            tail=b"",
            counters=None,
            last_error="session_meta_mismatch",
            error_signature=signature,
        )
        first = enrich(store)
        second = enrich(store)
        agents = [event.agent_id for event in store.events() if event.kind == "usage"]

    assert (first.accepted, first.failures) == (1, ())
    assert (second.accepted, second.failures, second.active_failures) == (0, (), ())
    assert agents == [CHILD]


@pytest.mark.skipif(os.name != "nt", reason="Win32 verbatim path spelling")
def test_verbatim_windows_spelling_registers_the_same_source(tmp_path):
    project = uuid4()
    session = "session-1"
    transcript = tmp_path / "rollout.jsonl"
    write_rollout(transcript, session, [usage(session, "response-1", total=10, output=4)])
    verbatim = Path("\\\\?\\" + str(transcript))
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, transcript))
        store.put(hook(project, session, verbatim))
        sources = store.transcript_sources()

    assert [source["path"] for source in sources] == [str(transcript)]


def test_source_failure_is_safe_isolated_and_not_retried_until_change(tmp_path, monkeypatch):
    project = uuid4()
    bad_session = "bad-session"
    good_session = "good-session"
    bad = tmp_path / "bad.jsonl"
    good = tmp_path / "good.jsonl"
    write_rollout(bad, bad_session, [usage(bad_session, "response-bad", total=10, output=4)])
    write_rollout(good, good_session, [usage(good_session, "response-good", total=12, output=5)])
    original = transcripts._usage_event
    secret = f"prompt and command output from {bad}"

    def failing_usage_event(store, source, *args, **kwargs):
        if source["session_id"] == bad_session:
            raise ValueError(secret)
        return original(store, source, *args, **kwargs)

    monkeypatch.setattr(transcripts, "_usage_event", failing_usage_event)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, bad_session, bad))
        store.put(hook(project, good_session, good))

        first = enrich(store)
        second = enrich(store)
        events = store.events()
        sources = store.transcript_sources()

    assert first.accepted == 1
    assert first.failures == ("transcript_source_invalid",)
    assert first.active_failures == ("transcript_source_invalid",)
    assert not second
    assert second.failures == ()
    assert second.active_failures == ("transcript_source_invalid",)
    assert set(first.failures) <= FAILURE_CODES
    assert [event.native_event_id for event in events if event.kind == "usage"] == ["response-good"]
    gaps = [event for event in events if event.kind == "observation.gap"]
    assert len(gaps) == 1
    assert codex(gaps[0])["reason"] == "transcript_source_invalid"
    bad_source = next(source for source in sources if source["session_id"] == bad_session)
    assert bad_source["last_error"] == "transcript_source_invalid"
    assert secret not in repr((gaps, bad_source, first))


def test_stale_transcript_failure_stays_diagnostic_without_degrading(tmp_path):
    project = uuid4()
    session = "session-1"
    # A directory is a failure that cannot heal; an absent file is only unobserved.
    not_a_file = tmp_path / "not-a-file.jsonl"
    not_a_file.mkdir()
    observed_at = datetime(2026, 9, 7, tzinfo=UTC)
    cutoff = datetime(2026, 9, 7, 0, 15, tzinfo=UTC)
    with Store(tmp_path / "data", project) as store:
        store.put(hook(project, session, not_a_file, received_at=observed_at))
        first = enrich(store, active_failure_since=cutoff)
        sources = store.transcript_sources()

        store.put(
            hook(project, session, not_a_file, received_at=datetime(2026, 9, 7, 0, 20, tzinfo=UTC))
        )
        refreshed = enrich(store, active_failure_since=cutoff)

    assert first.failures == ("transcript_path_not_regular",)
    assert first.active_failures == ()
    assert sources[0]["last_error"] == "transcript_path_not_regular"
    assert refreshed.failures == ()
    assert refreshed.active_failures == ("transcript_path_not_regular",)


def test_source_listing_failure_returns_only_an_allowlisted_code():
    secret = "private transcript path and output"

    class BrokenStore:
        def transcript_sources(self):
            raise OSError(secret)

    result = enrich(cast(Store, BrokenStore()))

    assert not result
    assert result.failures == ("transcript_sources_unavailable",)
    assert result.active_failures == ("transcript_sources_unavailable",)
    assert set(result.failures) <= FAILURE_CODES
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("private transcript path"), "transcript_io_unavailable"),
        (StorageError("private database detail"), "transcript_storage_unavailable"),
        (ValueError("private raw record"), "transcript_enrichment_invalid"),
    ],
)
def test_daemon_enrichment_exception_codes_are_fixed(error, expected):
    assert failure_code(error) == expected
    assert expected in FAILURE_CODES
