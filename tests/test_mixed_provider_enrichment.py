"""Simultaneous Codex + Claude enrichment stays isolated (WD-022b item 4)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from test_claude_transcripts import assistant as claude_assistant
from test_claude_transcripts import write_transcript
from test_transcripts import usage as codex_usage
from test_transcripts import write_rollout

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich

CLAUDE_SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CODEX_SESSION = "codex-session-1"


def codex_hook(project, transcript: Path) -> Envelope:
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=CODEX_SESSION,
        kind="turn.end",
        source="hook",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        payload={"codex": {"transcript_path": str(transcript)}},
    )


def claude_hook(project, transcript: Path) -> Envelope:
    return Envelope(
        provider="claude",
        project_id=project,
        session_id=CLAUDE_SESSION,
        kind="turn.end",
        source="hook",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        payload={"claude": {"transcript_path": str(transcript)}},
    )


def _seed(tmp_path):
    project = uuid4()
    codex_transcript = tmp_path / "rollout.jsonl"
    claude_transcript = tmp_path / "claude.jsonl"
    write_rollout(
        codex_transcript,
        CODEX_SESSION,
        [
            codex_usage(CODEX_SESSION, "response-1", total=10, output=4),
            codex_usage(CODEX_SESSION, "response-2", total=16, output=6),
        ],
    )
    write_transcript(
        claude_transcript,
        [
            claude_assistant(CLAUDE_SESSION, "req_A", block=0, output_tokens=7),
            claude_assistant(CLAUDE_SESSION, "req_A", block=1, output_tokens=7),
            claude_assistant(CLAUDE_SESSION, "req_B", block=0, output_tokens=9),
        ],
    )
    return project, codex_transcript, claude_transcript


def test_one_pass_processes_both_providers_without_crosstalk(tmp_path):
    project, codex_transcript, claude_transcript = _seed(tmp_path)
    with Store(tmp_path / "data", project) as store:
        store.put(codex_hook(project, codex_transcript))
        store.put(claude_hook(project, claude_transcript))
        assert enrich(store).accepted == 4  # 2 codex responses + 2 distinct claude requests
        assert enrich(store).accepted == 0
        usage = [event for event in store.events() if event.kind == "usage"]

    by_provider = {"codex": [], "claude": []}
    for event in usage:
        by_provider[event.provider].append(event)
        assert set(event.payload) == {event.provider}
    assert len(by_provider["codex"]) == 2
    assert len(by_provider["claude"]) == 2
    # Codex keeps its cumulative scope; Claude carries only a per-response body.
    assert all("cumulative" in event.payload["codex"]["usage"] for event in by_provider["codex"])
    assert all(
        set(event.payload["claude"]["usage"]) == {"response"} for event in by_provider["claude"]
    )


def test_a_claude_failure_does_not_stop_codex_in_the_same_pass(tmp_path):
    project, codex_transcript, _ = _seed(tmp_path)
    broken_claude = tmp_path / "broken.jsonl"
    broken_claude.write_text(
        json.dumps(claude_assistant("other-session", "req_X")) + "\n", encoding="utf-8"
    )
    with Store(tmp_path / "data", project) as store:
        store.put(codex_hook(project, codex_transcript))
        store.put(claude_hook(project, broken_claude))
        result = enrich(store)
        usage = [event for event in store.events() if event.kind == "usage"]
        gaps = [event for event in store.events() if event.kind == "observation.gap"]

    assert result.accepted == 2
    assert [event.provider for event in usage] == ["codex", "codex"]
    assert [event.provider for event in gaps] == ["claude"]


def test_retention_and_purge_clear_both_providers(tmp_path):
    project, codex_transcript, claude_transcript = _seed(tmp_path)
    agent_transcript = tmp_path / "agent.jsonl"
    write_transcript(
        agent_transcript, [claude_assistant("child-session", "req_child", output_tokens=3)]
    )
    with Store(tmp_path / "data", project) as store:
        store.put(codex_hook(project, codex_transcript))
        store.put(claude_hook(project, claude_transcript))
        store.put(
            Envelope(
                provider="claude",
                project_id=project,
                session_id=CLAUDE_SESSION,
                agent_id="agent-3",
                kind="agent.end",
                source="hook",
                received_at=datetime(2026, 9, 9, tzinfo=UTC),
                payload={"claude": {"agent_transcript_path": str(agent_transcript)}},
            )
        )
        assert len(store.transcript_sources()) == 3
        enrich(store)
        removed = store.purge_provider_session("claude", CLAUDE_SESSION)
        assert removed >= 1
        remaining = store.transcript_sources()

    # The parent purge also removed the "#"-encoded subagent reader row.
    assert [source["provider"] for source in remaining] == ["codex"]
