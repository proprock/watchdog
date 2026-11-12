"""Tests for the Claude Code parser/importer with synthetic fixtures."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.ingest.claude_code import import_claude_source
from wdi.ingest.projects import ProjectResolver

MACHINE = "m1"
SID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> Repository:
    repository = Repository(conn)
    repository.ensure_machine(MACHINE)
    return repository


@pytest.fixture
def resolver(repo: Repository) -> ProjectResolver:
    return ProjectResolver(repo, MACHINE, aliases={})


def write_jsonl(path: Path, objs: list[dict[str, Any]], partial: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs) + partial
    path.write_text(text, encoding="utf-8", newline="\n")


def user_prompt(text: str, ts: str, uuid: str = "u1") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "cwd": "C:/Repos/my-app",
        "gitBranch": "main",
        "version": "2.1.111",
        "entrypoint": "claude-desktop",
        "message": {"role": "user", "content": text},
    }


def assistant_msg(
    ts: str,
    uuid: str,
    text: str = "ok",
    tool_uses: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    stop_reason: str | None = "end_turn",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content += tool_uses or []
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "stop_reason": stop_reason,
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
            "content": content,
        },
    }


def tool_result_msg(
    call_id: str, text: str, is_error: bool = False, ts: str = "2026-06-01T10:00:02Z"
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"res-{call_id}",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": is_error,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }


def basic_session_lines() -> list[dict[str, Any]]:
    return [
        {"type": "ai-title", "aiTitle": "Test session", "sessionId": SID},
        user_prompt("сделай фичу", "2026-06-01T10:00:00Z"),
        assistant_msg(
            "2026-06-01T10:00:01Z",
            "a1",
            tool_uses=[
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "C:/Repos/my-app/main.py"},
                }
            ],
        ),
        tool_result_msg("t1", "file contents"),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": "2026-06-01T10:00:03Z",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 170822,
                "postTokens": 11677,
                "durationMs": 133247,
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-06-01T10:00:03Z",
            "hookCount": 2,
            "hookErrors": ["lint failed"],
            "hookInfos": [{"command": "uv run ruff check ."}, {"command": "uv run pytest"}],
            "hasOutput": True,
        },
        {
            "type": "system",
            "subtype": "api_error",
            "error": "overloaded",
            "timestamp": "2026-06-01T10:00:04Z",
            "apiErrorStatus": 529,
            "retryAttempt": 2,
            "maxRetries": 10,
            "retryInMs": 4000,
        },
        assistant_msg(
            "2026-06-01T10:00:05Z",
            "a2",
            text="готово",
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 3,
                    "ephemeral_1h_input_tokens": 4,
                },
                "iterations": [{"input_tokens": 6}, {"input_tokens": 4}],
                "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
            },
        ),
    ]


def test_import_basic_session(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    write_jsonl(root / "slug" / f"{SID}.jsonl", basic_session_lines())

    stats = import_claude_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 1
    assert stats.sessions == 1
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (SID,)).fetchone()
    assert session["agent"] == "claude"
    assert session["title"] == "Test session"
    assert session["cwd"] == "C:/Repos/my-app"
    assert session["git_branch"] == "main"
    assert session["cli_version"] == "2.1.111"
    assert session["model"] == "claude-opus-4-8"
    assert session["user_msg_count"] == 1
    assert session["assistant_msg_count"] == 2
    assert session["tool_call_count"] == 1
    assert session["input_tokens"] == 20
    assert session["output_tokens"] == 40
    assert session["cache_read_tokens"] == 10
    assert session["cache_creation_tokens"] == 14
    assert session["cache_creation_5m_tokens"] == 3
    assert session["cache_creation_1h_tokens"] == 4
    assert session["usage_iteration_count"] == 2
    assert session["hook_count"] == 2
    assert session["hook_error_count"] == 1
    assert "uv run pytest" in session["hook_commands"]
    assert session["hook_has_output_count"] == 1
    assert session["compaction_count"] == 1
    assert session["api_error_count"] == 1
    assert session["started_at"] == "2026-06-01T10:00:00Z"
    assert session["ended_at"] == "2026-06-01T10:00:05Z"
    assert session["duration_ms"] == 5000
    assert session["entrypoint"] == "claude-desktop"
    assert session["web_search_requests"] == 2
    assert session["web_fetch_requests"] == 1
    assert session["is_noise"] == 0

    project = conn.execute(
        "SELECT canonical_name FROM projects WHERE project_id = ?", (session["project_id"],)
    ).fetchone()
    assert project["canonical_name"] == "my-app"

    call = conn.execute("SELECT * FROM tool_calls WHERE session_id = ?", (SID,)).fetchone()
    assert call["call_id"] == "t1"
    assert call["tool_name"] == "Read"
    assert call["category"] == "read"
    assert call["target"] == "C:/Repos/my-app/main.py"
    assert call["is_error"] == 0
    assert call["output_preview"] == "file contents"
    assert call["output_bytes"] == len(b"file contents")
    # Latency derived from tool_use ts (10:00:01Z) -> tool_result ts (10:00:02Z).
    assert call["duration_ms"] == 1000

    assistant_row = conn.execute(
        "SELECT stop_reason, extra FROM messages WHERE message_id = 'a2'"
    ).fetchone()
    assert assistant_row["stop_reason"] == "end_turn"
    a2_extra = json.loads(assistant_row["extra"])
    assert a2_extra["web_search_requests"] == 2
    assert a2_extra["web_fetch_requests"] == 1

    compact = conn.execute(
        "SELECT extra FROM messages WHERE session_id = ? AND role = 'system'"
        " AND json_extract(extra, '$.subtype') = 'compact_boundary'",
        (SID,),
    ).fetchone()
    compact_extra = json.loads(compact["extra"])
    assert compact_extra["trigger"] == "auto"
    assert compact_extra["pre_tokens"] == 170822
    assert compact_extra["post_tokens"] == 11677
    assert compact_extra["duration_ms"] == 133247

    api_error = conn.execute(
        "SELECT extra FROM messages WHERE session_id = ? AND role = 'system'"
        " AND json_extract(extra, '$.subtype') = 'api_error'",
        (SID,),
    ).fetchone()
    error_extra = json.loads(api_error["extra"])
    assert error_extra["api_error_status"] == 529
    assert error_extra["retry_attempt"] == 2
    assert error_extra["max_retries"] == 10
    assert error_extra["retry_in_ms"] == 4000


def test_claude_context_events_capture_calls_tools_and_compaction(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    write_jsonl(root / "slug" / f"{SID}.jsonl", basic_session_lines())

    import_claude_source(repo, resolver, root, MACHINE)

    rows = conn.execute(
        "SELECT * FROM context_events WHERE session_id = ? ORDER BY idx", (SID,)
    ).fetchall()
    calls = [row for row in rows if row["event_type"] == "model_call"]
    assert len(calls) == 2
    assert calls[0]["input_tokens"] == 10
    assert any(row["event_type"] == "tool_input" and row["tool_name"] == "Read" for row in rows)
    assert any(
        row["event_type"] == "tool_output" and row["category"] == "code_file" for row in rows
    )
    compact = next(row for row in rows if row["event_type"] == "compact_boundary")
    meta = json.loads(compact["extra"])
    assert meta["trigger"] == "auto"
    assert meta["pre_tokens"] == 170822
    assert meta["post_tokens"] == 11677


def test_tool_result_error_marks_call(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    lines = [
        assistant_msg(
            "2026-06-01T10:00:01Z",
            "a1",
            tool_uses=[
                {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "exit 1"}}
            ],
        ),
        tool_result_msg("t9", "boom", is_error=True),
    ]
    write_jsonl(root / "slug" / f"{SID}.jsonl", lines)

    import_claude_source(repo, resolver, root, MACHINE)

    call = conn.execute(
        "SELECT is_error, category, target FROM tool_calls WHERE call_id = 't9'"
    ).fetchone()
    assert call["is_error"] == 1
    assert call["category"] == "exec"
    assert call["target"] == "exit 1"


def test_subagent_transcript_imported_as_stream(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    main_lines = [
        user_prompt("исследуй код", "2026-06-01T10:00:00Z"),
        assistant_msg(
            "2026-06-01T10:00:01Z",
            "a1",
            tool_uses=[
                {
                    "type": "tool_use",
                    "id": "task1",
                    "name": "Task",
                    "input": {"subagent_type": "Explore", "prompt": "find stuff"},
                }
            ],
        ),
        {
            "type": "user",
            "uuid": "u-task",
            "timestamp": "2026-06-01T10:01:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "task1",
                        "content": [{"type": "text", "text": "report"}],
                    }
                ],
            },
            "toolUseResult": {
                "agentId": "abc123",
                "agentType": "Explore",
                "prompt": "find stuff",
                "sourceToolAssistantUUID": "a1",
                "status": "completed",
                "totalDurationMs": 28197,
                "totalTokens": 48181,
                "totalToolUseCount": 14,
                "toolStats": {
                    "readCount": 5,
                    "searchCount": 2,
                    "bashCount": 7,
                    "editFileCount": 0,
                    "linesAdded": 0,
                    "linesRemoved": 0,
                },
            },
        },
    ]
    write_jsonl(root / "slug" / f"{SID}.jsonl", main_lines)
    agent_lines = [
        {
            "type": "user",
            "uuid": "su1",
            "timestamp": "2026-06-01T10:00:02Z",
            "agentId": "abc123",
            "isSidechain": True,
            "message": {"role": "user", "content": "find stuff"},
        },
        assistant_msg(
            "2026-06-01T10:00:03Z",
            "sa1",
            tool_uses=[
                {"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "TODO"}}
            ],
            usage={"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    write_jsonl(root / "slug" / SID / "subagents" / "agent-abc123.jsonl", agent_lines)

    stats = import_claude_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 2
    run = conn.execute("SELECT * FROM subagent_runs WHERE agent_run_id = 'abc123'").fetchone()
    assert run["session_id"] == SID
    assert run["agent_type"] == "Explore"
    assert run["total_tokens"] == 48181
    assert run["read_count"] == 5
    assert json.loads(run["extra"])["source_tool_assistant_uuid"] == "a1"

    sub_messages = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND stream_id = 'abc123'", (SID,)
    ).fetchone()[0]
    assert sub_messages == 2
    sub_calls = conn.execute(
        "SELECT tool_name FROM tool_calls WHERE session_id = ? AND stream_id = 'abc123'", (SID,)
    ).fetchall()
    assert [c["tool_name"] for c in sub_calls] == ["Grep"]

    # Session aggregates count only the main stream; subagent tokens stay separate.
    session = conn.execute(
        "SELECT input_tokens, tool_call_count FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["input_tokens"] == 10
    assert session["tool_call_count"] == 1


def test_incremental_append(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    path = root / "slug" / f"{SID}.jsonl"
    lines = basic_session_lines()
    write_jsonl(path, lines[:3])

    import_claude_source(repo, resolver, root, MACHINE)
    first_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    write_jsonl(path, lines)  # rewrite is fine: file only grows
    stats = import_claude_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 1
    second_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert second_count > first_count
    indexes = [
        r["idx"]
        for r in conn.execute(
            "SELECT idx FROM messages WHERE stream_id = 'main' ORDER BY idx"
        ).fetchall()
    ]
    assert indexes == list(range(len(indexes)))

    repeat = import_claude_source(repo, resolver, root, MACHINE)
    assert repeat.files_imported == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == second_count


def test_partial_trailing_line_deferred(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    path = root / "slug" / f"{SID}.jsonl"
    lines = basic_session_lines()
    partial = json.dumps(lines[-1])[:20]
    write_jsonl(path, lines[:2], partial=partial)

    import_claude_source(repo, resolver, root, MACHINE)

    state = repo.get_import_state(MACHINE, str(path))
    assert state is not None
    assert state.byte_offset < path.stat().st_size

    write_jsonl(path, lines)
    import_claude_source(repo, resolver, root, MACHINE)
    count = conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'").fetchone()[0]
    assert count == 2


def test_shrunk_file_full_reparse_without_duplicates(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    path = root / "slug" / f"{SID}.jsonl"
    write_jsonl(path, basic_session_lines())
    import_claude_source(repo, resolver, root, MACHINE)

    write_jsonl(path, basic_session_lines()[:3])
    stats = import_claude_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 1
    session = conn.execute(
        "SELECT assistant_msg_count, user_msg_count FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["assistant_msg_count"] == 1
    assert session["user_msg_count"] == 1


def test_sdk_cli_session_flagged_as_noise(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-06-01T10:00:00Z",
            "cwd": "C:/observers",
            "entrypoint": "sdk-cli",
            "message": {"role": "user", "content": "observe"},
        },
        assistant_msg("2026-06-01T10:00:01Z", "a1", text="observed"),
    ]
    write_jsonl(root / "slug" / f"{SID}.jsonl", lines)

    import_claude_source(repo, resolver, root, MACHINE)

    session = conn.execute(
        "SELECT entrypoint, is_noise FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["entrypoint"] == "sdk-cli"
    assert session["is_noise"] == 1


def test_truncated_output_stop_reason_recorded(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    lines = [
        user_prompt("напиши много кода", "2026-06-01T10:00:00Z"),
        assistant_msg("2026-06-01T10:00:01Z", "a1", stop_reason="max_tokens"),
    ]
    write_jsonl(root / "slug" / f"{SID}.jsonl", lines)

    import_claude_source(repo, resolver, root, MACHINE)

    row = conn.execute("SELECT stop_reason FROM messages WHERE message_id = 'a1'").fetchone()
    assert row["stop_reason"] == "max_tokens"


def test_broken_line_is_skipped_with_warning(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    path = root / "slug" / f"{SID}.jsonl"
    good = json.dumps(user_prompt("привет", "2026-06-01T10:00:00Z"))
    path.parent.mkdir(parents=True)
    path.write_text(f"{good}\nnot json at all\n", encoding="utf-8", newline="\n")

    stats = import_claude_source(repo, resolver, root, MACHINE)

    assert stats.warnings == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_fragment_completed_in_later_import_does_not_double_usage(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "claude"
    path = root / "slug" / f"{SID}.jsonl"
    first = {
        "type": "assistant",
        "uuid": "a1",
        "message": {
            "id": "provider-1",
            "model": "claude-test",
            "content": [{"type": "text", "text": "part1"}],
            "usage": {"input_tokens": 100, "output_tokens": 2},
        },
    }
    second = {
        "type": "assistant",
        "uuid": "a2",
        "message": {
            "id": "provider-1",
            "model": "claude-test",
            "content": [{"type": "text", "text": "part2"}],
            "usage": {"input_tokens": 100, "output_tokens": 8},
        },
    }
    write_jsonl(path, [first])
    import_claude_source(repo, resolver, root, MACHINE)
    write_jsonl(path, [first, second])
    import_claude_source(repo, resolver, root, MACHINE)
    row = conn.execute(
        "SELECT input_tokens,output_tokens FROM sessions WHERE session_id=?", (SID,)
    ).fetchone()
    assert tuple(row) == (100, 8)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM context_events WHERE event_type='model_call'"
        ).fetchone()[0]
        == 1
    )
