"""Exercise the Watchdog import through the Insights service and CLI."""

import hashlib
import json
import re
import shutil
from compression import zstd
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner
from wdi.cli import app
from wdi.config import Settings
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.ingest.service import run_import
from wdi.llm.analyzers import ANALYZERS
from wdi.web import create_app

from agent_watchdog import capture_store, config

MACHINE = UUID("11111111-1111-4111-8111-111111111111")
PROJECT = UUID("22222222-2222-4222-8222-222222222222")
SESSION = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
SUBAGENT = "abc123"
STAMP = "2026-11-12T12:00:00Z"


def _options(
    tmp_path: Path, *, database: str = "insights", files: bool = False, stores: bool = True
) -> dict[str, Any]:
    return {
        "machine_id": str(MACHINE),
        "db_path": str(tmp_path / f"{database}.sqlite"),
        "sources": {
            "claude_code": {"enabled": files, "root": str(tmp_path / "claude")},
            "codex": {"enabled": files, "root": str(tmp_path / "codex")},
        },
        "watchdog": {"data_dirs": [str(tmp_path / "watchdog")] if stores else []},
    }


def _project_dir(tmp_path: Path, project_id: UUID = PROJECT) -> Path:
    return tmp_path / "watchdog" / "projects" / str(project_id)


def _store(
    tmp_path: Path,
    *,
    project_id: UUID = PROJECT,
    manifest: str = "current",
) -> capture_store.CaptureStore:
    project_dir = _project_dir(tmp_path, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    if manifest != "missing":
        project = config.Project(id=project_id, root=tmp_path / "manifest-root")
        capture_store.write_manifest(project_dir, machine_id=MACHINE, project=project)
        path = project_dir / capture_store.STORE_MANIFEST_FILENAME
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update(hostname="capture-host", os="capture-os")
        if manifest == "future":
            document["contract"] += 1
        path.write_text(json.dumps(document), encoding="utf-8")
    return capture_store.CaptureStore(project_dir / capture_store.CAPTURE_FILENAME)


def _append(
    store: capture_store.CaptureStore,
    lines: list[bytes],
    *,
    provider: str = "claude",
    session_id: str = SESSION,
    stream_id: str = "main",
    generation: int = 0,
    seal: bool = False,
) -> None:
    size = sum(len(line) + 1 for line in lines)
    with store.connect() as db:
        stream = db.execute(
            """
            INSERT INTO streams (
                provider, session_id, stream_id, generation, source_path, source_kind,
                head_len, head_digest, byte_offset, record_count, raw_bytes,
                first_seen, last_growth, last_read, state
            ) VALUES (?, ?, ?, ?, 'capture.jsonl', 'hook', 0, '', ?, ?, ?, ?, ?, ?, 'open')
            ON CONFLICT (provider, session_id, stream_id, generation) DO UPDATE SET
                record_count = streams.record_count + excluded.record_count,
                raw_bytes = streams.raw_bytes + excluded.raw_bytes,
                byte_offset = streams.byte_offset + excluded.byte_offset
            RETURNING stream_pk, record_count
            """,
            (
                provider,
                session_id,
                stream_id,
                generation,
                size,
                len(lines),
                size,
                STAMP,
                STAMP,
                STAMP,
            ),
        ).fetchone()
        assert stream is not None
        db.executemany(
            "INSERT INTO tail (stream_pk, record_seq, line) VALUES (?, ?, ?)",
            ((stream[0], seq, line) for seq, line in enumerate(lines, stream[1] - len(lines))),
        )
        if seal:
            capture_store.seal_tail(db, stream[0], idle=True)


def _line(**record: Any) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


def _prompt(text: str, *, cwd: Path | None = None) -> bytes:
    fields = {"cwd": str(cwd)} if cwd is not None else {}
    return _line(
        type="user", uuid=text, timestamp=STAMP, message={"role": "user", "content": text}, **fields
    )


def _claude_file(tmp_path: Path, lines: list[bytes], *, session_id: str = SESSION) -> Path:
    path = tmp_path / "claude" / "synthetic" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + b"\n" for line in lines))
    return path


def _rows(settings: Settings, query: str, *params: object) -> list[tuple[Any, ...]]:
    with closing(connect(settings.db_path)) as db:
        return [tuple(row) for row in db.execute(query, params)]


def _previews(settings: Settings) -> list[tuple[Any, ...]]:
    return _rows(
        settings, "SELECT stream_id, content_preview FROM messages ORDER BY stream_id, idx"
    )


def _key(provider: str = "claude", session: str = SESSION, stream: str = "main") -> str:
    return f"wd://{MACHINE}/{PROJECT}/{provider}/{session}/{stream}"


@pytest.mark.parametrize("analyzer", ["prompt-quality", "summary", "workflow-recommendations"])
def test_analyzer_reads_captured_prompt(tmp_path: Path, analyzer: str) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    prompt = "Original developer prompt " + "x" * 900 + " beyond the preview"
    _append(_store(tmp_path), [_prompt(prompt)])
    run_import(settings)

    with closing(connect(settings.db_path)) as db:
        rendered = ANALYZERS[analyzer].build_prompt(
            db, SESSION, data_dirs=settings.watchdog.data_dirs
        )
    assert prompt in rendered


@pytest.mark.parametrize("stream_id", ["main", SUBAGENT])
@pytest.mark.parametrize("side_file", ["inline", "captured", "missing"])
def test_tool_detail_reads_capture_without_provider_file(
    tmp_path: Path, stream_id: str, side_file: str
) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    store = _store(tmp_path)
    full_input = "input " + "x" * 900 + " end"
    full_output = "output " + "é" * 900 + " end"
    filename = "result-1.txt"
    provider_file = tmp_path / SESSION / "tool-results" / filename
    reference = (
        f"<persisted-output>\nOutput too large (42.1KB). Full output saved to: {provider_file}"
        "\n\nPreview (first 2KB):\npreview text\n</persisted-output>"
    )
    _append(store, [_prompt("main prompt")])
    _append(
        store,
        [
            _line(
                type="assistant",
                uuid="call",
                timestamp=STAMP,
                message={
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Bash",
                            "input": {"command": full_input},
                        },
                    ],
                },
            ),
            _line(
                type="user",
                uuid="result",
                timestamp=STAMP,
                message={
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": full_output if side_file == "inline" else reference,
                        },
                    ],
                },
            ),
        ],
        stream_id=stream_id,
    )
    if side_file == "captured":
        data = full_output.encode()
        with store.connect() as db:
            db.execute(
                """INSERT INTO side_files VALUES ('claude', ?, ?, ?, ?, 'zstd', ?, ?)""",
                (
                    SESSION,
                    f"tool-results/{filename}",
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    zstd.compress(data),
                    STAMP,
                ),
            )
    run_import(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/sessions/{SESSION}/stream/{stream_id}/call/call-1/full")

    assert response.status_code == 200
    assert full_input in response.text
    assert "preview text" not in response.text
    if side_file == "missing":
        assert "Full output unavailable" in response.text
    else:
        assert full_output in response.text


def test_watchdog_import_matches_file_projection(tmp_path: Path) -> None:
    lines = [
        _prompt("Read café", cwd=tmp_path / "parsed-cwd"),
        _line(
            type="assistant",
            uuid="assistant",
            timestamp="2026-11-12T12:00:01Z",
            message={
                "role": "assistant",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "content": [
                    {"type": "text", "text": "Reading."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "a"},
                    },
                ],
            },
        ),
    ]
    file = _claude_file(tmp_path, lines)
    _append(_store(tmp_path), lines, seal=True)
    from_file = Settings.model_validate(
        _options(tmp_path, database="file", files=True, stores=False)
    )
    from_store = Settings.model_validate(_options(tmp_path))

    run_import(from_file)
    run_import(from_store)

    assert _rows(from_file, "SELECT origin, source_path FROM sessions") == [("file", str(file))]
    assert _rows(from_store, "SELECT origin, source_path FROM sessions") == [("watchdog", _key())]
    with (
        closing(connect(from_file.db_path)) as file_db,
        closing(connect(from_store.db_path)) as wd_db,
    ):
        file_session = dict(file_db.execute("SELECT * FROM sessions").fetchone())
        store_session = dict(wd_db.execute("SELECT * FROM sessions").fetchone())
        for field in ("origin", "source_path"):
            del file_session[field]
            del store_session[field]
        assert file_session == store_session
    for table in ("messages", "tool_calls", "context_events"):
        expected = _rows(from_file, f"SELECT * FROM {table} ORDER BY idx")
        assert expected
        assert _rows(from_store, f"SELECT * FROM {table} ORDER BY idx") == expected
    assert _rows(from_store, "SELECT machine_id, hostname, os FROM machines") == [
        (str(MACHINE), "capture-host", "capture-os")
    ]
    assert _rows(from_store, "SELECT path FROM project_paths") == [(str(tmp_path / "parsed-cwd"),)]


def test_watchdog_import_replays_a_growing_stream(tmp_path: Path) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    store = _store(tmp_path)
    first = _prompt("first")
    second = _prompt("second")
    _append(store, [first], seal=True)
    run_import(settings)

    _append(store, [second])
    run_import(settings)
    run_import(settings)

    assert _previews(settings) == [("main", "first"), ("main", "second")]
    assert _rows(settings, "SELECT path FROM project_paths") == [(str(tmp_path / "manifest-root"),)]
    assert _rows(
        settings, "SELECT source_path, file_size, file_mtime, byte_offset FROM import_state"
    ) == [(_key(), len(first) + len(second) + 2, 0, 2)]


def test_watchdog_import_replaces_rows_from_an_old_generation(tmp_path: Path) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    store = _store(tmp_path)
    _append(store, [_prompt("previous generation has more bytes"), _prompt("old second message")])
    run_import(settings)

    replacement = _prompt("replacement")
    _append(store, [replacement], generation=1)
    run_import(settings)

    assert _previews(settings) == [("main", "replacement")]
    assert _rows(
        settings, "SELECT source_path, file_size, file_mtime, byte_offset FROM import_state"
    ) == [(_key(), len(replacement) + 1, 1, 1)]


def test_watchdog_import_preserves_claude_subagent_stream_identity(tmp_path: Path) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    store = _store(tmp_path)
    _append(store, [_prompt("child stream")], stream_id=SUBAGENT)
    _append(store, [_prompt("main stream")])

    run_import(settings)

    assert _rows(settings, "SELECT session_id, origin, source_path FROM sessions") == [
        (SESSION, "watchdog", _key())
    ]
    assert _previews(settings) == [(SUBAGENT, "child stream"), ("main", "main stream")]
    assert _rows(settings, "SELECT source_path FROM import_state ORDER BY source_path") == [
        (_key(stream=SUBAGENT),),
        (_key(),),
    ]


def test_stale_copy_cannot_replace_newer_generation_during_full_import(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(store, [_prompt("old first"), _prompt("old second")])
    stale = tmp_path / "copy"
    shutil.copytree(tmp_path / "watchdog", stale)
    _append(store, [_prompt("new generation")], generation=1)
    options = _options(tmp_path)
    options["watchdog"]["data_dirs"].insert(0, str(stale))
    settings = Settings.model_validate(options)
    run_import(settings)
    assert _previews(settings) == [("main", "new generation")]

    options["watchdog"]["data_dirs"] = [str(stale)]
    run_import(Settings.model_validate(options), full=True)
    assert _previews(settings) == [("main", "new generation")]


def test_claude_subagent_capture_cannot_displace_main_file(tmp_path: Path) -> None:
    path = _claude_file(tmp_path, [_prompt("main file")])
    settings = Settings.model_validate(_options(tmp_path, files=True))
    run_import(settings)
    _append(_store(tmp_path), [_prompt("captured child")], stream_id=SUBAGENT)

    run_import(settings)

    assert _rows(settings, "SELECT origin, source_path FROM sessions") == [("file", str(path))]
    assert _previews(settings) == [("main", "main file")]


def test_watchdog_import_creates_a_separate_session_for_each_codex_thread(tmp_path: Path) -> None:
    settings = Settings.model_validate(_options(tmp_path))
    store = _store(tmp_path)
    for session, parent in [(SESSION, None), (OTHER, SESSION)]:
        source = {"subagent": {"other": "guardian"}} if parent else "vscode"
        _append(
            store,
            [
                _line(
                    type="session_meta",
                    timestamp=STAMP,
                    payload={"id": session, "source": source, "parent_thread_id": parent},
                ),
                _line(
                    type="event_msg",
                    timestamp=STAMP,
                    payload={"type": "user_message", "message": f"Thread {session}"},
                ),
            ],
            provider="codex",
            session_id=session,
        )

    run_import(settings)

    assert _rows(
        settings,
        "SELECT session_id, parent_session_id, origin, source_path "
        "FROM sessions ORDER BY session_id",
    ) == [
        (SESSION, None, "watchdog", _key("codex")),
        (OTHER, SESSION, "watchdog", _key("codex", OTHER)),
    ]
    assert _rows(settings, "SELECT session_id, stream_id FROM messages ORDER BY session_id") == [
        (SESSION, "main"),
        (OTHER, "main"),
    ]


def test_watchdog_takes_over_a_file_session_and_removes_its_cursor(tmp_path: Path) -> None:
    file = _claude_file(tmp_path, [_prompt("file content")])
    settings = Settings.model_validate(_options(tmp_path, files=True))
    run_import(settings)
    assert _rows(settings, "SELECT source_path FROM import_state") == [(str(file),)]
    with closing(connect(settings.db_path)) as db:
        Repository(db).upsert_llm_analysis(
            SESSION,
            str(MACHINE),
            analysis_type="session",
            prompt_version="1",
            model="test-model",
            score=0.5,
            result={"summary": "Saved analysis"},
        )
    analysis = _rows(settings, "SELECT * FROM llm_analyses")
    assert analysis

    _append(_store(tmp_path), [_prompt("stored content")])
    run_import(settings)

    assert _rows(settings, "SELECT origin, source_path FROM sessions") == [("watchdog", _key())]
    assert _previews(settings) == [("main", "stored content")]
    assert _rows(settings, "SELECT source_path FROM import_state") == [(_key(),)]
    assert _rows(settings, "SELECT * FROM llm_analyses") == analysis


def test_file_import_skips_a_session_already_imported_from_watchdog(tmp_path: Path) -> None:
    _append(_store(tmp_path), [_prompt("stored content")])
    settings = Settings.model_validate(_options(tmp_path))
    run_import(settings)

    _claude_file(tmp_path, [_prompt("later file content")])
    run_import(Settings.model_validate(_options(tmp_path, files=True, stores=False)))

    assert _previews(settings) == [("main", "stored content")]
    assert _rows(settings, "SELECT source_path FROM import_state") == [(_key(),)]


def test_watchdog_leaves_legacy_session_rows_untouched(tmp_path: Path) -> None:
    _claude_file(tmp_path, [_prompt("legacy content")])
    settings = Settings.model_validate(_options(tmp_path, files=True))
    run_import(settings)
    with closing(connect(settings.db_path)) as db, db:
        db.execute("UPDATE sessions SET origin = 'legacy'")
    original = _rows(settings, "SELECT * FROM sessions")
    cursor = _rows(settings, "SELECT * FROM import_state")

    store = _store(tmp_path)
    _append(store, [_prompt("new content for legacy session")])
    _append(store, [_prompt("ordinary captured content")], session_id=OTHER)
    run_import(settings)

    assert _rows(settings, "SELECT * FROM sessions WHERE session_id = ?", SESSION) == original
    assert _rows(
        settings, "SELECT content_preview FROM messages WHERE session_id = ?", SESSION
    ) == [("legacy content",)]
    assert (
        _rows(settings, "SELECT * FROM import_state WHERE source_path NOT LIKE 'wd://%'") == cursor
    )
    assert _rows(settings, "SELECT session_id FROM sessions WHERE origin = 'watchdog'") == [
        (OTHER,)
    ]


@pytest.mark.parametrize("manifest", ["missing", "future"])
def test_watchdog_skips_stores_with_unusable_manifests(tmp_path: Path, manifest: str) -> None:
    options = _options(tmp_path)
    settings = Settings.model_validate(options)
    _append(_store(tmp_path), [_prompt("supported store")])
    project = UUID("55555555-5555-4555-8555-555555555555")
    _append(
        _store(tmp_path, project_id=project, manifest=manifest),
        [_prompt("unusable store")],
        session_id=OTHER,
    )
    run_import(settings)

    assert _previews(settings) == [("main", "supported store")]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(options), encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert str(project) in result.output
    assert ("manifest" if manifest == "missing" else "unsupported") in result.output.lower()


def test_doctor_reports_file_only_sessions_by_provider(tmp_path: Path) -> None:
    _claude_file(tmp_path, [_prompt("will be captured")])
    _claude_file(tmp_path, [_prompt("still file only")], session_id=OTHER)
    run_import(Settings.model_validate(_options(tmp_path, files=True, stores=False)))
    _append(_store(tmp_path), [_prompt("now captured")])
    options = _options(tmp_path, files=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(options), encoding="utf-8")

    result = CliRunner().invoke(app, ["doctor", "-c", str(config_path)])

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "watchdog") in result.output
    assert str(PROJECT) in result.output
    assert "file-only" in result.output.lower()
    assert re.search(r"(?im)^.*\bclaude\b.*\b1\b", result.output)
    assert re.search(r"(?im)^.*\bcodex\b.*\b0\b", result.output)
