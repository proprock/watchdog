import json
import sqlite3
from compression import zstd
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_watchdog import capture_store, config
from agent_watchdog.storage import Store

_NOW = "2026-11-12T12:00:00+00:00"


def _registered(tmp_path):
    from agent_watchdog import readapi

    reader = getattr(readapi, "registered_projects", None)
    assert callable(reader), "registered-project reader is required"
    project = config.Project(id=uuid4(), root=tmp_path / "checkout", alias="sample")
    config.save_config(tmp_path / "config.toml", config.Config(projects=(project,)))
    (registered,) = reader(tmp_path / "config.toml", tmp_path / "data")
    return registered


def test_registered_projects_includes_uncollected_projects(tmp_path):
    project = _registered(tmp_path)
    assert project.name == "sample"
    assert project.root == str(tmp_path / "checkout")
    assert project.path == tmp_path / "data" / "projects" / project.project_id
    assert not project.path.exists()


def test_registered_snapshot_is_read_only_consistent_and_manifest_optional(tmp_path):
    from agent_watchdog import readapi
    from agent_watchdog.storage import StorageError

    project = _registered(tmp_path)
    with pytest.raises(StorageError, match="no collected database"), readapi.snapshot(project):
        pass
    with Store(project.path, UUID(project.project_id)):
        pass
    with readapi.snapshot(project) as db:
        assert isinstance(db.execute("SELECT * FROM metadata").fetchone(), sqlite3.Row)
        with sqlite3.connect(project.path / "events.sqlite3") as writer:
            writer.execute("UPDATE metadata SET project_id=?", (str(uuid4()),))
        assert db.execute("SELECT project_id FROM metadata").fetchone()[0] == project.project_id
        db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden (id INTEGER)")
    with pytest.raises(StorageError, match="different project"), readapi.snapshot(project):
        pass


@pytest.mark.parametrize("schema", [0, 99])
def test_registered_snapshot_refuses_unsupported_schema(tmp_path, schema):
    from agent_watchdog import readapi
    from agent_watchdog.storage import StorageError

    project = _registered(tmp_path)
    with Store(project.path, UUID(project.project_id)):
        pass
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        db.execute(f"PRAGMA user_version={schema}")
    with (
        pytest.raises(StorageError, match="Unsupported database schema"),
        readapi.snapshot(project),
    ):
        pass


@pytest.mark.parametrize("schema", [7, 8, 9])
def test_findings_old_schemas_and_missing_data_are_explicit(tmp_path, schema):
    from agent_watchdog import readapi
    from agent_watchdog.analysis import analyze
    from agent_watchdog.events import Envelope

    project = _registered(tmp_path)
    assert readapi.findings(project) == {"state": "unavailable", "reason": "no_collected_database"}
    with Store(project.path, UUID(project.project_id)) as store:
        store.put(
            Envelope(
                project_id=UUID(project.project_id),
                provider="codex",
                session_id="s",
                kind="session.end",
                source="hook",
            )
        )
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        db.execute(f"PRAGMA user_version={schema}")
        if schema == 8:
            db.execute("ALTER TABLE session_analysis DROP COLUMN rule_version")
    result = readapi.findings(project)
    if schema < 8:
        assert result == {"state": "unsupported", "reason": "schema_predates_v8"}
    else:
        assert result["state"] == "ready" and result["pending_sessions"] == 1
        assert result["as_of"] is None
        assert result["by_provider"] == {
            p: {"by_rule": {}, "gaps": analyze([])["gaps"]} for p in ("codex", "claude")
        }
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == schema


def test_findings_filter_current_version_and_deduplicate_fingerprints(tmp_path, monkeypatch):
    from agent_watchdog import analysis, readapi
    from agent_watchdog.events import Envelope

    project = _registered(tmp_path)
    with Store(project.path, UUID(project.project_id)) as store:
        for sid in ("s1", "s2"):
            store.put(
                Envelope(
                    project_id=UUID(project.project_id),
                    provider="codex",
                    session_id=sid,
                    kind="session.end",
                    source="hook",
                )
            )
            store.refresh_session_findings("codex", sid)
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        for sid in ("s1", "s2"):
            db.execute(
                "INSERT INTO session_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("codex", sid, "diff_oscillation", analysis.RULE_VERSION, "shared", 9, "[]", _NOW),
            )
        db.execute(
            "UPDATE session_analysis SET gaps_json='[\"retained_gap\"]', computed_at=?", (_NOW,)
        )
    result = readapi.findings(project)
    assert result["as_of"] == _NOW
    assert result["window"] == "all" and result["pending_sessions"] == 0
    assert result["by_provider"]["codex"] == {
        "by_rule": {"diff_oscillation": 1},
        "gaps": ["retained_gap"],
    }
    monkeypatch.setattr(analysis, "RULE_VERSION", "new-rule-version")
    refreshed = readapi.findings(project)
    assert refreshed["pending_sessions"] == 2 and refreshed["as_of"] is None
    assert refreshed["by_provider"]["codex"]["by_rule"] == {}


@pytest.mark.parametrize("label_schema", ["missing", "legacy", "current"])
def test_session_review_sanitizes_provider_events_and_preserves_labels(tmp_path, label_schema):
    from agent_watchdog import readapi
    from agent_watchdog.events import Envelope

    project = _registered(tmp_path)
    with Store(project.path, UUID(project.project_id)) as store:
        for provider in ("codex", "claude"):
            store.put(
                Envelope(
                    project_id=UUID(project.project_id),
                    provider=provider,
                    session_id="s",
                    kind="session.end",
                    source="hook",
                    payload={provider: {"token": "private-value"}},
                )
            )
        store.label(
            "codex",
            "s",
            outcome="success",
            task_type="review",
            progress_state="progress",
            reviewer_note="Reviewed",
        )
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        if label_schema == "missing":
            db.execute("DROP TABLE session_labels")
        elif label_schema == "legacy":
            db.execute("ALTER TABLE session_labels DROP COLUMN progress_state")
            db.execute("ALTER TABLE session_labels DROP COLUMN reviewer_note")
    (record,) = readapi.session_review(project, "codex", ["s", "s"])
    assert record["session_id"] == "s" and record["event_count"] == 1
    assert record["events"][0].provider == "codex"
    assert record["events"][0].payload == {"codex": {"token": "[REDACTED]"}}
    assert record["label"] == {
        "task_outcome": "unknown" if label_schema == "missing" else "success",
        "task_type": None if label_schema == "missing" else "review",
        "progress_state": "progress" if label_schema == "current" else None,
        "reviewer_note": "Reviewed" if label_schema == "current" else None,
    }
    assert ("task_outcome_unknown" in record["gaps"]) == (label_schema == "missing")
    with sqlite3.connect(project.path / "events.sqlite3") as db:
        assert "private-value" in db.execute("SELECT envelope FROM events LIMIT 1").fetchone()[0]


@pytest.mark.parametrize(
    "provider,session_ids,message",
    [
        ("other", ["s"], "Unsupported provider"),
        ("codex", [], "Select at least"),
        ("codex", [" "], "Select at least"),
        ("codex", ["unknown"], "Unknown session"),
    ],
)
def test_session_review_validates_selection(tmp_path, provider, session_ids, message):
    from agent_watchdog import readapi
    from agent_watchdog.storage import StorageError

    project = _registered(tmp_path)
    with Store(project.path, UUID(project.project_id)):
        pass
    with pytest.raises(StorageError, match=message):
        readapi.session_review(project, provider, session_ids)


def test_environment_readers_preserve_columns_and_blob_bytes(tmp_path):
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    store = capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME)
    raw = b"synthetic patch\x00"
    digest = sha256(raw).hexdigest()
    with store.connect() as db:
        db.execute(
            "INSERT INTO session_env (provider, session_id, captured_at, harness, files, watchdog) "
            "VALUES ('codex', 's', ?, ?, ?, ?)",
            (_NOW, '{"model":"example"}', "[]", "{}"),
        )
        db.execute(
            "INSERT INTO checkout_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "checkout",
                _NOW,
                "codex",
                "s",
                "stop",
                None,
                "head",
                None,
                None,
                2,
                None,
                0,
                digest,
                None,
            ),
        )
        db.execute(
            "INSERT INTO blobs VALUES (?, ?, 'zstd', ?)", (digest, len(raw), zstd.compress(raw))
        )
    (info,) = readapi.stores(tmp_path)
    env = list(readapi.session_env(info))
    outcomes = list(readapi.checkout_states(info))
    with store.connect(write=False) as db:
        assert env == [dict(db.execute("SELECT * FROM session_env").fetchone())]
        assert outcomes == [dict(db.execute("SELECT * FROM checkout_states").fetchone())]
    assert readapi.blob_bytes(info, digest) == raw
    with pytest.raises(KeyError):
        readapi.blob_bytes(info, "missing")


def test_environment_readers_handle_missing_and_old_schema(tmp_path):
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    (info,) = readapi.stores(tmp_path)
    assert list(readapi.session_env(info)) == []
    assert list(readapi.checkout_states(info)) == []
    with pytest.raises(KeyError):
        readapi.blob_bytes(info, "missing")
    with sqlite3.connect(project_dir / capture_store.CAPTURE_FILENAME) as db:
        db.execute("PRAGMA user_version = 1")
    _replace_manifest_field(project_dir, "capture_schema", 1)
    (info,) = readapi.stores(tmp_path)
    assert list(readapi.session_env(info)) == []
    assert list(readapi.checkout_states(info)) == []


def test_schema_two_environment_has_null_initial_statistics(tmp_path):
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    with sqlite3.connect(project_dir / capture_store.CAPTURE_FILENAME) as db:
        for statement in capture_store.CAPTURE_ENV_SCHEMA:
            db.execute(statement)
        db.execute("PRAGMA user_version = 2")
        db.execute(
            "INSERT INTO session_env (provider, session_id, captured_at, harness, files, watchdog) "
            "VALUES ('codex', 's', 't', '{}', '[]', '{}')"
        )
    _replace_manifest_field(project_dir, "capture_schema", 2)
    (info,) = readapi.stores(tmp_path)
    (row,) = readapi.session_env(info)
    assert all(
        row[name] is None
        for name in (
            "initial_files_changed",
            "initial_insertions",
            "initial_deletions",
            "initial_untracked",
        )
    )
    assert list(readapi.checkout_states(info)) == []


def _manifest(data_dir: Path, name: str = "sample") -> Path:
    project = config.Project(id=uuid4(), root=data_dir.parent / name)
    project_dir = data_dir / "projects" / str(project.id)
    project_dir.mkdir(parents=True)
    capture_store.write_manifest(project_dir, machine_id=uuid4(), project=project)
    return project_dir


def _replace_manifest_field(project_dir: Path, field: str, value: int) -> None:
    path = project_dir / "store.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _database(project_dir: Path, kind: str) -> None:
    if kind == "events":
        with Store(project_dir, UUID(project_dir.name)):
            pass
    else:
        with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect():
            pass


def _stream(
    db: sqlite3.Connection,
    lines: list[bytes],
    *,
    generation: int = 0,
    stream_id: str = "main",
    session_id: str = "sample-session",
) -> int:
    row = db.execute(
        """
        INSERT INTO streams (
            provider, session_id, stream_id, generation, source_path, source_kind, cwd,
            head_len, head_digest, byte_offset, record_count, raw_bytes,
            first_seen, last_growth, last_read, state
        ) VALUES ('claude', ?, ?, ?, 'sample.jsonl', 'hook', 'sample-cwd',
                  0, '', 0, ?, ?, ?, ?, ?, 'open') RETURNING stream_pk
        """,
        (
            session_id,
            stream_id,
            generation,
            len(lines),
            sum(len(line) + 1 for line in lines),
            _NOW,
            _NOW,
            _NOW,
        ),
    ).fetchone()
    assert row is not None
    stream_pk = row[0]
    db.executemany(
        "INSERT INTO tail (stream_pk, record_seq, line) VALUES (?, ?, ?)",
        ((stream_pk, seq, line) for seq, line in enumerate(lines)),
    )
    return stream_pk


def test_stores_skips_missing_manifests_and_flags_future_contract(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    current = _manifest(tmp_path, "current")
    future = _manifest(tmp_path, "future")
    (tmp_path / "projects" / str(uuid4())).mkdir()
    _replace_manifest_field(future, "contract", 2)

    found = {str(info.project_id): info for info in readapi.stores(tmp_path)}
    assert set(found) == {current.name, future.name}
    assert found[current.name].supported is True
    assert found[current.name].contract == 1
    assert found[current.name].name == "current"
    assert found[current.name].root == str(tmp_path.parent / "current")
    assert found[future.name].supported is False
    assert found[future.name].contract == 2


@pytest.mark.parametrize("reader", ["events", "event_facts"])
def test_event_readers_page_by_parent_rowid_and_release_snapshot(tmp_path, monkeypatch, reader):
    from agent_watchdog import readapi
    from agent_watchdog.events import Envelope

    project_dir = _manifest(tmp_path)
    with Store(project_dir, UUID(project_dir.name)) as store:
        for number in range(3):
            store.put(
                Envelope(
                    provider="claude",
                    project_id=UUID(project_dir.name),
                    session_id="sample-session",
                    kind="tool.finish",
                    source="hook",
                    payload={
                        "claude": {"metadata": {"tool_name": "Read", "tool_duration_ms": number}}
                    },
                )
            )
    (info,) = readapi.stores(tmp_path)
    monkeypatch.setattr(readapi, "_PAGE_SIZE", 1)
    iterator = getattr(readapi, reader)(info, after_rowid=1)
    second = next(iterator)
    assert second["event_rowid"] == 2
    assert second["session_id"] == "sample-session"
    with sqlite3.connect(project_dir / "events.sqlite3", timeout=0.1) as db:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        third_id = db.execute("SELECT event_id FROM events WHERE rowid = 3").fetchone()[0]
        if reader == "event_facts":
            db.execute("UPDATE event_facts SET rowid = 99 WHERE event_id = ?", (third_id,))
    third = next(iterator)
    assert third["event_rowid"] == 3
    assert list(iterator) == []
    if reader == "event_facts":
        assert set(third) == {"event_rowid", *readapi.FACT_COLUMNS}
        assert third["tool_name"] == "Read"


@pytest.mark.parametrize("reader", ["events", "event_facts"])
def test_event_readers_handle_missing_database_and_reject_future_contract(tmp_path, reader):
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    (info,) = readapi.stores(tmp_path)
    assert list(getattr(readapi, reader)(info)) == []
    _replace_manifest_field(project_dir, "contract", 2)
    (future,) = readapi.stores(tmp_path)
    with pytest.raises(ValueError, match="(?i)contract"):
        list(getattr(readapi, reader)(future))


def test_project_without_capture_database_lists_no_transcripts(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    (info,) = readapi.stores(tmp_path)

    assert list(readapi.streams(info)) == []
    assert list(readapi.side_files(info, "claude", "sample-session")) == []
    with pytest.raises(FileNotFoundError), readapi.open_capture(info):
        pytest.fail("Opening a missing database succeeded")
    assert not (project_dir / capture_store.CAPTURE_FILENAME).exists()


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_is_read_only(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with open_database(info) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden (id INTEGER)")


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_refuses_future_contract(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    _replace_manifest_field(project_dir, "contract", 2)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with pytest.raises(ValueError, match="(?i)contract"), open_database(info):
        pytest.fail("A newer contract opened successfully")


@pytest.mark.parametrize("kind", ["events", "capture"])
def test_open_database_refuses_manifest_schema_mismatch(tmp_path: Path, kind: str) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    _database(project_dir, kind)
    _replace_manifest_field(project_dir, f"{kind}_schema", 123)
    (info,) = readapi.stores(tmp_path)
    open_database = readapi.open_events if kind == "events" else readapi.open_capture

    with pytest.raises(ValueError, match="(?i)(version|schema)"), open_database(info):
        pytest.fail("A database with the wrong schema version opened successfully")


def test_open_events_refuses_database_owned_by_another_project(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    with Store(project_dir, uuid4()):
        pass
    (info,) = readapi.stores(tmp_path)

    with pytest.raises(ValueError, match="(?i)project"), readapi.open_events(info):
        pytest.fail("A database owned by another project opened successfully")


def test_streams_yields_latest_generation_per_stream(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        latest = _stream(db, [b"one", b"two"], generation=1)
        _stream(db, [b"earlier"])
        subagent = _stream(db, [b"child"], stream_id="a12f")
    (info,) = readapi.stores(tmp_path)

    streams = {stream.stream_pk: stream for stream in readapi.streams(info)}
    assert set(streams) == {latest, subagent}
    main = streams[latest]
    assert (main.provider, main.session_id, main.stream_id) == ("claude", "sample-session", "main")
    assert (main.generation, main.record_count, main.raw_bytes) == (1, 2, 8)
    assert main.cwd == "sample-cwd"


@pytest.mark.parametrize("start_record", [0, 1, 2, 3, 4, 5, 6])
def test_lines_preserves_exact_bytes_from_sealed_chunks_and_tail(
    tmp_path: Path, start_record: int
) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    expected = [b"\xff\r", b"", b"123", b"4567", b"z"]
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        stream_pk = _stream(db, expected)
        capture_store.seal_tail(db, stream_pk, target_bytes=5)
        capture_store.seal_tail(db, stream_pk, target_bytes=5)
    (info,) = readapi.stores(tmp_path)

    assert (
        list(readapi.lines(info, stream_pk, start_record=start_record)) == expected[start_record:]
    )


@pytest.mark.parametrize("consumed", [1, 2])
def test_suspended_lines_iterator_allows_checkpoint_during_tail_seal(
    tmp_path: Path, consumed: int
) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    path = project_dir / capture_store.CAPTURE_FILENAME
    store = capture_store.CaptureStore(path)
    expected = [b"sealed", b"first tail", b"second tail", b"last tail"]
    with store.connect() as db:
        stream_pk = _stream(db, expected)
        capture_store.seal_tail(db, stream_pk, target_bytes=len(expected[0]) + 1)
    (info,) = readapi.stores(tmp_path)

    with closing(sqlite3.connect(path, timeout=0)) as checkpoint:
        checkpoint.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        records = readapi.lines(info, stream_pk)
        assert [next(records) for _ in range(consumed)] == expected[:consumed]
        with store.connect() as db:
            capture_store.seal_tail(db, stream_pk, idle=True)
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0
        assert tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
        assert list(records) == expected[consumed:]


def test_suspended_lines_rejects_a_reused_stream_key(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    store = capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME)
    with store.connect() as db:
        stream_pk = _stream(db, [b"first", b"original"])
        capture_store.seal_tail(db, stream_pk, target_bytes=6)
    (info,) = readapi.stores(tmp_path)
    records = readapi.lines(info, stream_pk)
    assert next(records) == b"first"

    store.purge("claude", "sample-session")
    with store.connect() as db:
        assert _stream(db, [b"other first", b"other tail"], session_id="replacement") == stream_pk

    with pytest.raises(ValueError, match="(?i)stream.*changed"):
        next(records)


def test_side_files_list_metadata_and_read_original_bytes(tmp_path: Path) -> None:
    from agent_watchdog import readapi

    project_dir = _manifest(tmp_path)
    path = "tool-results/output"
    expected = [b"initial\r\n", b"replacement\x00\xff"]
    digests = [sha256(data).hexdigest() for data in expected]
    with capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME).connect() as db:
        db.executemany(
            """
            INSERT INTO side_files (
                provider, session_id, rel_path, digest, raw_bytes, codec, data, captured_at
            ) VALUES (?, ?, ?, ?, ?, 'zstd', ?, ?)
            """,
            [
                ("claude", "sample-session", path, digest, len(data), zstd.compress(data), _NOW)
                for digest, data in zip(digests, expected, strict=True)
            ]
            + [("claude", "other-session", "other", "other", 0, zstd.compress(b""), _NOW)],
        )
    (info,) = readapi.stores(tmp_path)

    files = list(readapi.side_files(info, "claude", "sample-session"))
    assert {(file.rel_path, file.digest, file.raw_bytes, file.captured_at) for file in files} == {
        (path, digest, len(data), _NOW) for digest, data in zip(digests, expected, strict=True)
    }
    assert [
        readapi.side_file_bytes(info, "claude", "sample-session", path, digest)
        for digest in digests
    ] == expected
