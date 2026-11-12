import hashlib
from compression import zstd
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.storage import Store

NOW = datetime(2026, 11, 12, tzinfo=UTC)
SESSION = "12345678-1234-1234-1234-123456789abc"
ROLLOUT = f"rollout-2026-11-12T00-00-00-{SESSION}.jsonl"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "project", uuid4()) as project:
        yield project


def name_source(store, path, provider="codex", hook_session="hook", seen=NOW):
    store.connection.execute(
        "INSERT OR REPLACE INTO transcript_sources (provider, session_id, path, last_seen) "
        "VALUES (?, ?, ?, ?)",
        (provider, hook_session, str(path), seen.isoformat()),
    )


def captured(store):
    from agent_watchdog.capture_store import CAPTURE_FILENAME, CaptureStore, iter_lines

    with CaptureStore(store.root / CAPTURE_FILENAME).connect() as db:
        return [
            (dict(row), list(iter_lines(db, row["stream_pk"])))
            for row in db.execute("SELECT * FROM streams ORDER BY stream_pk")
        ]


def run(store, **kwargs):
    from agent_watchdog.capture import capture

    if "clock" not in kwargs:
        kwargs.setdefault("budget_seconds", 10)
    return capture(store, now=kwargs.pop("now", NOW), **kwargs)


@pytest.mark.parametrize(
    ("provider", "path", "expected"),
    [
        ("codex", ROLLOUT, (SESSION, "main")),
        ("codex", ROLLOUT + ".zst", (SESSION, "main")),
        ("codex", "unknown.jsonl", None),
        ("claude", "repo/session.jsonl", ("session", "main")),
        ("claude", "repo/session/subagents/agent-a12f.jsonl", ("session", "a12f")),
        ("claude", "repo/session/subagents/agent-nope.jsonl", None),
    ],
)
def test_stream_identity(provider, path, expected):
    from agent_watchdog.capture import stream_identity

    assert stream_identity(provider, Path(path)) == expected


def test_capture_resumes_at_complete_newline(store, tmp_path):
    path = tmp_path / ROLLOUT
    name_source(store, path)
    path.write_bytes(b' {"a":1}\r\n\xff\npartial')
    run(store)
    ((row, lines),) = captured(store)
    assert lines == [b' {"a":1}\r', b"\xff"]
    assert row["byte_offset"] == len(b' {"a":1}\r\n\xff\n')
    with path.open("ab") as stream:
        stream.write(b"\n\n")
    run(store, now=NOW + timedelta(seconds=1))
    ((row, lines),) = captured(store)
    assert b"".join(line + b"\n" for line in lines) == path.read_bytes()
    assert row["hook_session_id"] == "hook"
    assert row["head_digest"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 9


@pytest.mark.parametrize("replacement", [b"a\n", b"Z\nb\nc\n"])
def test_capture_preserves_replaced_generation(store, tmp_path, replacement):
    path = tmp_path / ROLLOUT
    path.write_bytes(b"a\nb\n")
    name_source(store, path)
    run(store)
    path.write_bytes(replacement)
    run(store, now=NOW + timedelta(seconds=1))
    rows = captured(store)
    assert [row["generation"] for row, _ in rows] == [0, 1]
    assert rows[0][1] == [b"a", b"b"]
    assert rows[1][1] == replacement.splitlines()


def test_capture_follows_zstd_replacement_then_marks_missing(store, tmp_path):
    path = tmp_path / ROLLOUT
    path.write_bytes(b"first\n")
    name_source(store, path)
    run(store)
    path.unlink()
    compressed = path.with_suffix(".jsonl.zst")
    compressed.write_bytes(zstd.compress(b"first\nsecond\n"))
    run(store, now=NOW + timedelta(seconds=1))
    ((row, lines),) = captured(store)
    assert row["generation"] == 0
    assert lines == [b"first", b"second"]
    compressed.unlink()
    run(store, now=NOW + timedelta(seconds=2))
    ((row, lines),) = captured(store)
    assert row["state"] == "missing"
    assert lines == [b"first", b"second"]


def test_capture_skips_oversized_line_only_after_newline(store, tmp_path, monkeypatch):
    from agent_watchdog import capture

    monkeypatch.setattr(capture, "MAX_LINE_BYTES", 4)
    path = tmp_path / ROLLOUT
    path.write_bytes(b"12345")
    name_source(store, path)
    run(store)
    ((row, lines),) = captured(store)
    assert row["skipped_lines"] == row["byte_offset"] == 0
    path.write_bytes(b"12345\nok\n")
    run(store, now=NOW + timedelta(seconds=1))
    ((row, lines),) = captured(store)
    assert row["skipped_lines"] == 1
    assert row["skipped_bytes"] == 6
    assert lines == [b"ok"]


def test_capture_seals_full_and_idle_tails(store, tmp_path):
    from agent_watchdog.capture_store import CaptureStore

    path = tmp_path / ROLLOUT
    path.write_bytes(b"abcd\nx\n")
    name_source(store, path)
    run(store, target_bytes=5)
    with CaptureStore(store.root / "capture.sqlite3").connect() as db:
        assert db.execute("SELECT record_count FROM chunks").fetchall()[0][0] == 1
        assert db.execute("SELECT line FROM tail").fetchall()[0][0] == b"x"
    run(store, now=NOW + timedelta(hours=24), target_bytes=5)
    with CaptureStore(store.root / "capture.sqlite3").connect() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM tail").fetchone()[0] == 0


def test_capture_retains_claude_subagent_and_side_file_versions(store, tmp_path):
    from agent_watchdog.capture_store import CaptureStore

    main = tmp_path / "session.jsonl"
    main.write_bytes(b"main\n")
    child = tmp_path / "session" / "subagents" / "agent-a12f.jsonl"
    child.parent.mkdir(parents=True)
    child.write_bytes(b"child\n")
    artifact = tmp_path / "session" / "tool-results" / "out"
    artifact.parent.mkdir()
    artifact.write_bytes(b"old")
    name_source(store, main, provider="claude", hook_session="session")
    run(store, budget_seconds=10)
    artifact.write_bytes(b"new")
    run(store, now=NOW + timedelta(seconds=1), budget_seconds=10)
    assert {(row["stream_id"], tuple(lines)) for row, lines in captured(store)} == {
        ("main", (b"main",)),
        ("a12f", (b"child",)),
    }
    with CaptureStore(store.root / "capture.sqlite3").connect() as db:
        rows = db.execute("SELECT rel_path, data FROM side_files").fetchall()
    assert {(path, zstd.decompress(data)) for path, data in rows} == {
        ("tool-results/out", b"old"),
        ("tool-results/out", b"new"),
    }


def test_capture_excludes_disabled_content(tmp_path):
    path = tmp_path / ROLLOUT
    path.write_bytes(b"private\n")
    with Store(tmp_path / "project", uuid4(), limits=Limits(capture_content=False)) as store:
        name_source(store, path)
        run(store)
        assert not (store.root / "capture.sqlite3").exists()


def test_capture_ignores_stale_and_unrecognized_hook_names(store, tmp_path):
    stale = tmp_path / ROLLOUT
    stale.write_bytes(b"stale\n")
    name_source(store, stale, seen=NOW - timedelta(hours=2))
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_bytes(b"unknown\n")
    name_source(store, unknown)
    assert run(store).unrecognized_name == 1
    assert not (store.root / "capture.sqlite3").exists()


def test_capture_budget_resumes_next_stream(store, tmp_path):
    other = f"rollout-2026-11-12T00-00-00-{uuid4()}.jsonl"
    for name in (ROLLOUT, other):
        path = tmp_path / name
        path.write_bytes(b"content\n")
        name_source(store, path)
    for second in (0, 1):
        ticks = iter([0, 0.01])
        result = run(
            store, clock=lambda ticks=ticks: next(ticks, 0.11), now=NOW + timedelta(seconds=second)
        )
        assert result.pending
        assert len(captured(store)) == second + 1


def test_capture_refuses_project_quota(store, tmp_path, monkeypatch):
    from agent_watchdog import resources

    path = tmp_path / ROLLOUT
    path.write_bytes(b"content\n")
    name_source(store, path)
    monkeypatch.setattr(resources, "available", lambda *_: 0)
    assert run(store).degraded
    assert not (store.root / "capture.sqlite3").exists()


def test_capture_budget_resumes_claude_directory_discovery(store, tmp_path, monkeypatch):
    from agent_watchdog import capture

    main = tmp_path / "session.jsonl"
    main.write_bytes(b"main\n")
    directory = tmp_path / "session" / "subagents"
    directory.mkdir(parents=True)
    for identity in ("a1", "a2", "a3"):
        (directory / f"agent-{identity}.jsonl").write_bytes(b"child\n")
    name_source(store, main, provider="claude")
    original = capture._files
    elapsed = 0.0

    def slow_files(directory):
        nonlocal elapsed
        for path in original(directory):
            elapsed += 0.06
            yield path

    monkeypatch.setattr(capture, "_files", slow_files)
    cursor = capture.CaptureCursor()
    for second in range(10):
        run(store, clock=lambda: elapsed, cursor=cursor, now=NOW + timedelta(seconds=second))
    assert {row["stream_id"] for row, _ in captured(store)} == {"main", "a1", "a2", "a3"}


def test_capture_quota_refusal_yields_to_next_stream(store, tmp_path, monkeypatch):
    from agent_watchdog import resources
    from agent_watchdog.capture import WRITE_OVERHEAD

    large = tmp_path / ROLLOUT
    small = tmp_path / "rollout-2026-11-12T00-00-00-99999999-9999-9999-9999-999999999999.jsonl"
    large.write_bytes(b"x" * 400 + b"\n")
    small.write_bytes(b"ok\n")
    name_source(store, large)
    name_source(store, small)
    monkeypatch.setattr(resources, "available", lambda *_: WRITE_OVERHEAD + 500)
    assert run(store).degraded
    run(store, now=NOW + timedelta(seconds=1))
    assert [lines for _, lines in captured(store) if lines] == [[b"ok"]]


def test_capture_retries_truncated_zstd_without_losing_records(store, tmp_path):
    path = tmp_path / ROLLOUT
    path.write_bytes(b"first\n")
    name_source(store, path)
    run(store)
    path.unlink()
    compressed = path.with_suffix(".jsonl.zst")
    data = zstd.compress(b"first\nsecond\n")
    compressed.write_bytes(data[:-1])
    assert run(store, now=NOW + timedelta(seconds=1)).degraded
    assert captured(store)[0][1] == [b"first"]
    compressed.write_bytes(data)
    assert not run(store, now=NOW + timedelta(seconds=2)).degraded
    assert captured(store)[0][1] == [b"first", b"second"]


def test_purge_removes_capture_named_by_hook_session(store, tmp_path):
    path = tmp_path / ROLLOUT
    path.write_bytes(b"child\n")
    store.put(
        Envelope(
            provider="codex",
            project_id=store.project_id,
            kind="turn.start",
            source="hook",
            session_id="parent",
            received_at=NOW,
            payload={"codex": {"transcript_path": str(path)}},
        )
    )
    run(store)
    assert len(captured(store)) == 1
    assert store.purge_provider_session("codex", "parent") == 1
    run(store)
    assert not captured(store)
    assert path.read_bytes() == b"child\n"
