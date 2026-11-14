"""Harness version comparison reads only the captured session metadata."""

import json
import random
import uuid
from compression import zstd
from pathlib import Path

import pytest

from agent_watchdog.capture_store import CaptureStore
from agent_watchdog.events import Envelope
from agent_watchdog.harness_version import compare

SESSION = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def event(tmp_path, monkeypatch):
    source = tmp_path / "provider-session.jsonl"
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path != source, "comparison opened the provider transcript"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    return Envelope(
        provider="codex",
        project_id=uuid.uuid4(),
        session_id=SESSION,
        kind="turn.end",
        source="hook",
        payload={"codex": {"transcript_path": str(source)}},
        delivery={"codex_control_profile": "windows-cli-0.160.0"},
    )


def _metadata(version="0.160.0", session=SESSION, **extra):
    return json.dumps({"type": "session_meta", "payload": {"id": session, "cli_version": version, **extra}}).encode()


def _sealed(path, event, raw, *, source=None, hook_session=SESSION):
    with CaptureStore(path).connect() as db:
        stream = db.execute(
            "INSERT INTO streams (provider, session_id, stream_id, generation, hook_session_id, "
            "source_path, source_kind, head_len, head_digest, byte_offset, record_count, raw_bytes, "
            "first_seen, last_growth, last_read, state) "
            "VALUES ('codex', ?, 'main', 0, ?, ?, 'hook', 0, '', ?, 1, ?, '', '', '', 'sealed') "
            "RETURNING stream_pk",
            (SESSION, hook_session, source or event.payload["codex"]["transcript_path"], len(raw), len(raw)),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO chunks (stream_pk, chunk_seq, first_record, record_count, raw_bytes, codec, data) "
            "VALUES (?, 0, 0, 1, ?, 'zstd', ?)",
            (stream, len(raw), zstd.compress(raw)),
        )


@pytest.mark.parametrize(
    ("version", "status"), [("0.160.0", "match"), ("0.999.0", "drift")]
)
def test_compare_reads_sealed_metadata(tmp_path, event, version, status):
    path = tmp_path / "capture.sqlite3"
    _sealed(path, event, _metadata(version) + b"\n")
    assert compare(path, event) == (status, "0.160.0", version)


@pytest.mark.parametrize("state", ["missing", "corrupt"])
def test_compare_reports_unknown_for_unreadable_capture(tmp_path, event, state):
    path = tmp_path / "capture.sqlite3"
    if state == "corrupt":
        path.write_bytes(b"not a sqlite database")
    assert compare(path, event) == ("unknown", "0.160.0", None)


@pytest.mark.parametrize("mismatch", ["metadata_id", "source_path", "hook_session"])
def test_compare_rejects_metadata_from_another_session_or_source(tmp_path, event, mismatch):
    path = tmp_path / "capture.sqlite3"
    metadata = _metadata(session="other" if mismatch == "metadata_id" else SESSION)
    _sealed(
        path,
        event,
        metadata + b"\n",
        source=str(tmp_path / "another.jsonl") if mismatch == "source_path" else None,
        hook_session="other" if mismatch == "hook_session" else SESSION,
    )
    assert compare(path, event) == ("unknown", "0.160.0", None)


def test_compare_reads_first_metadata_from_large_compressed_chunk(tmp_path, event):
    path = tmp_path / "capture.sqlite3"
    raw = _metadata() + b"\n" + random.Random(1).randbytes(200_000) + b"\n"
    assert len(zstd.compress(raw)) > 128 * 1024
    _sealed(path, event, raw)
    assert compare(path, event) == ("match", "0.160.0", "0.160.0")


def test_compare_reports_unknown_for_oversized_metadata(tmp_path, event):
    path = tmp_path / "capture.sqlite3"
    _sealed(path, event, _metadata(padding="x" * 70_000) + b"\n")
    assert compare(path, event) == ("unknown", "0.160.0", None)


def test_capture_schedules_refresh_when_existing_stream_gains_hook_session(tmp_path):
    from agent_watchdog.capture import Source, capture, capture_file
    from agent_watchdog.config import Limits
    from agent_watchdog.storage import Store

    root = tmp_path / "project"
    root.mkdir()
    source = tmp_path / f"rollout-2026-11-12T00-00-00-{SESSION}.jsonl"
    source.write_bytes(_metadata("0.999.0") + b"\n")
    event = Envelope(
        provider="codex",
        project_id=uuid.uuid4(),
        session_id=SESSION,
        kind="turn.end",
        source="hook",
        payload={"codex": {"transcript_path": str(source)}},
        delivery={"codex_control_profile": "windows-cli-0.160.0"},
    )
    assert capture_file(root, Limits(), Source("codex", SESSION, "main", source, None, "backfill"))[0]
    path = root / "capture.sqlite3"
    assert compare(path, event) == ("unknown", "0.160.0", None)
    with Store(root, event.project_id) as store:
        store.put(event)
        result = capture(store, budget_seconds=10)
    assert ("codex", SESSION) in result.changed_sessions
    assert compare(path, event) == ("drift", "0.160.0", "0.999.0")
