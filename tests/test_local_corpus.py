"""Regression checks over the user's real transcripts in the git-ignored ``tests/local/`` (WD-117).

Layout: ``tests/local/claude/**/*.jsonl`` and ``tests/local/codex/**/*.jsonl``. Without
those files every test here is skipped, and nothing in this module reads the network.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store
from agent_watchdog.transcripts import enrich


def session_id(provider: str, path: Path) -> str | None:
    """Return the session id a transcript declares in its first line that carries one."""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if provider == "claude":
                found = record.get("sessionId")
            else:
                payload = record.get("payload")
                found = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(found, str) and found:
                return found
    return None


def test_real_transcript_is_enriched_without_a_failure(corpus_transcript, tmp_path):
    provider, path = corpus_transcript
    session = session_id(provider, path)
    if session is None:
        pytest.skip(f"no declared session id in {path.name}")
    project = uuid4()
    event = Envelope(
        provider=provider,
        project_id=project,
        session_id=session,
        kind="turn.end",
        source="hook",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        payload={provider: {"transcript_path": str(path)}},
    )
    with Store(tmp_path / "data", project) as store:
        assert store.put(event)
        result = enrich(store)

    assert result.failures == ()
