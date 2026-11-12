"""Tests for the Codex rollout parser/importer with synthetic fixtures."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from wdi.db.connection import apply_migrations, connect
from wdi.db.repository import Repository
from wdi.ingest.codex import import_codex_source
from wdi.ingest.projects import ProjectResolver

MACHINE = "m1"
SID = "019eae50-c169-73c1-81ba-eeba321f3026"
CHILD_SID = "019eccab-43fb-7de1-b7cc-1d7e513f802f"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "wdi.sqlite")
    apply_migrations(connection)
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


def env(ts: str, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": ts, "type": record_type, "payload": payload}


def rollout_lines() -> list[dict[str, Any]]:
    return [
        env(
            "2026-06-09T20:00:00Z",
            "session_meta",
            {
                "id": SID,
                "timestamp": "2026-06-09T20:00:00Z",
                "cwd": "C:\\Repos\\demo-app",
                "cli_version": "0.137.0-alpha.4",
                "originator": "Codex Desktop",
                "git": {"branch": "dev", "commit_hash": "abc"},
            },
        ),
        env(
            "2026-06-09T20:00:01Z",
            "turn_context",
            {
                "turn_id": "t1",
                "model": "gpt-5.4",
                "effort": "high",
                "approval_policy": "on-request",
                "sandbox_policy": {"type": "workspace-write", "network_access": False},
                "collaboration_mode": "Default",
                "developer_instructions": "dev rules",
                "user_instructions": "user rules",
            },
        ),
        env(
            "2026-06-09T20:00:02Z",
            "event_msg",
            {"type": "user_message", "message": "почини тесты"},
        ),
        env(
            "2026-06-09T20:00:03Z",
            "response_item",
            {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({"command": "pytest -q"}),
            },
        ),
        env(
            "2026-06-09T20:00:05Z",
            "event_msg",
            {
                "type": "exec_command_end",
                "call_id": "call_1",
                "exit_code": 1,
                "duration": {"secs": 2, "nanos": 500_000_000},
                "stdout": "1 failed\n",
                "stderr": "warning\n",
                "aggregated_output": "1 failed",
                "parsed_cmd": [{"program": "pytest", "args": ["-q"]}],
            },
        ),
        env(
            "2026-06-09T20:00:05Z",
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_1",
                # Header duplicates exec_command_end telemetry with coarser wall time;
                # the exec event arrived first and must win.
                "output": "Exit code: 1\nWall time: 2.9 seconds\nOutput:\n1 failed",
            },
        ),
        env(
            "2026-06-09T20:00:06Z",
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call_2",
                "input": "*** Begin Patch ...",
            },
        ),
        env(
            "2026-06-09T20:00:07Z",
            "event_msg",
            {
                "type": "patch_apply_end",
                "call_id": "call_2",
                "success": True,
                "stdout": "ok",
                "changes": [
                    {
                        "path": "src/app.py",
                        "unified_diff": "@@\n-old\n+new\n+another\n",
                    }
                ],
            },
        ),
        env(
            "2026-06-09T20:00:08Z",
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "починил"}],
            },
        ),
        env(
            "2026-06-09T20:00:08Z",
            "response_item",
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "instructions"}],
            },
        ),
        env(
            "2026-06-09T20:00:09Z",
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 200},
                    "last_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 400,
                        "output_tokens": 200,
                        "reasoning_output_tokens": 50,
                    },
                    "model_context_window": 258400,
                },
                "rate_limits": {
                    "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": "soon"},
                    "secondary": {
                        "used_percent": 33.0,
                        "window_minutes": 10080,
                        "resets_at": "later",
                    },
                },
            },
        ),
        env("2026-06-09T20:00:09Z", "event_msg", {"type": "token_count", "info": None}),
        env(
            "2026-06-09T20:00:10Z",
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "t1",
                "duration_ms": 8000,
                "time_to_first_token_ms": 1200,
            },
        ),
        env("2026-06-09T20:00:11Z", "event_msg", {"type": "context_compacted"}),
        env(
            "2026-06-09T20:00:12Z",
            "event_msg",
            {"type": "error", "message": "stream disconnected"},
        ),
        env(
            "2026-06-09T20:00:13Z",
            "event_msg",
            {"type": "thread_name_updated", "thread_name": "Починка тестов"},
        ),
    ]


def write_rollout(
    root: Path,
    objs: list[dict[str, Any]],
    sid: str = SID,
    ts: str = "2026-06-09T20-00-00",
) -> Path:
    path = root / "2026" / "06" / "09" / f"rollout-{ts}-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs),
        encoding="utf-8",
        newline="\n",
    )
    return path


def meta(sid: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": sid,
        "timestamp": "2026-06-09T20:00:00Z",
        "cwd": "C:\\Repos\\demo-app",
        "cli_version": "0.140.0-alpha.2",
        "originator": "Codex Desktop",
    }
    payload.update(overrides)
    return env("2026-06-09T20:00:00Z", "session_meta", payload)


def test_import_codex_session(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(root, rollout_lines())

    stats = import_codex_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 1
    assert stats.sessions == 1
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (SID,)).fetchone()
    assert session["agent"] == "codex"
    assert session["cwd"] == "C:\\Repos\\demo-app"
    assert session["cli_version"] == "0.137.0-alpha.4"
    assert session["originator"] == "Codex Desktop"
    assert session["git_branch"] == "dev"
    assert session["title"] == "Починка тестов"
    assert session["model"] == "gpt-5.4"
    assert session["context_window"] == 258400
    assert session["task_duration_ms"] == 8000
    assert session["time_to_first_token_ms"] == 1200
    assert session["rate_limit_primary_used_percent"] == 12.5
    assert session["rate_limit_secondary_used_percent"] == 33.0
    assert session["effort"] == "high"
    assert session["approval_policy"] == "on-request"
    assert session["sandbox_policy_type"] == "workspace-write"
    assert session["collaboration_mode"] == "Default"
    assert session["developer_instructions_hash"] == hashlib.sha256(b"dev rules").hexdigest()[:12]
    assert session["user_instructions_hash"] == hashlib.sha256(b"user rules").hexdigest()[:12]
    assert session["user_msg_count"] == 1
    assert session["assistant_msg_count"] == 1
    assert session["tool_call_count"] == 2
    assert session["input_tokens"] == 1000
    assert session["output_tokens"] == 200
    assert session["cache_read_tokens"] == 400
    assert session["reasoning_tokens"] == 50
    assert session["cache_creation_tokens"] is None
    assert session["compaction_count"] == 1
    assert session["api_error_count"] == 1
    assert session["peak_context_tokens"] == 1000
    assert session["turn_count"] == 1
    assert session["entrypoint"] is None

    project = conn.execute(
        "SELECT canonical_name FROM projects WHERE project_id = ?", (session["project_id"],)
    ).fetchone()
    assert project["canonical_name"] == "demo-app"


def test_codex_import_leaves_legacy_session_untouched(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(root, rollout_lines())
    # Allow this regression test to exercise the old importer before the schema update.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "origin" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN origin TEXT")
    repo.ensure_session(SID, MACHINE, "codex")
    conn.execute(
        "UPDATE sessions SET origin = 'legacy', title = 'archived' WHERE session_id = ?", (SID,)
    )
    conn.execute(
        "INSERT INTO messages (session_id, machine_id, idx, role, content_preview) "
        "VALUES (?, ?, 0, 'user', 'original message')",
        (SID, MACHINE),
    )
    conn.commit()

    stats = import_codex_source(repo, resolver, root, MACHINE)

    assert stats.files_imported == 0
    assert (
        conn.execute("SELECT title FROM sessions WHERE session_id = ?", (SID,)).fetchone()[0]
        == "archived"
    )
    assert [row[0] for row in conn.execute("SELECT content_preview FROM messages")] == [
        "original message"
    ]


def test_codex_tool_calls_correlated(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(root, rollout_lines())

    import_codex_source(repo, resolver, root, MACHINE)

    shell = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_1'").fetchone()
    assert shell["tool_name"] == "shell_command"
    assert shell["category"] == "exec"
    assert shell["target"] == "pytest -q"
    assert shell["exit_code"] == 1
    assert shell["is_error"] == 1
    assert shell["duration_ms"] == 2500
    assert shell["command_kind"] == "pytest"
    assert shell["stdout_bytes"] == len(b"1 failed\n")
    assert shell["stderr_bytes"] == len(b"warning\n")
    assert shell["output_bytes"] == len(b"1 failed")
    assert shell["output_preview"] == "1 failed"
    assert json.loads(shell["extra"])["parsed_cmd"] == [{"program": "pytest", "args": ["-q"]}]

    patch = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_2'").fetchone()
    assert patch["tool_name"] == "apply_patch"
    assert patch["category"] == "edit"
    assert patch["is_error"] == 0
    assert patch["patch_success"] == 1
    assert patch["changed_file_count"] == 1
    assert patch["lines_added"] == 2
    assert patch["lines_removed"] == 1


def test_codex_incremental_append(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    lines = rollout_lines()
    path = write_rollout(root, lines[:4])
    import_codex_source(repo, resolver, root, MACHINE)

    write_rollout(root, lines)
    stats = import_codex_source(repo, resolver, root, MACHINE)
    assert stats.files_imported == 1

    repeat = import_codex_source(repo, resolver, root, MACHINE)
    assert repeat.files_imported == 0

    session = conn.execute(
        "SELECT input_tokens, tool_call_count FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["input_tokens"] == 1000
    assert session["tool_call_count"] == 2
    state = repo.get_import_state(MACHINE, str(path))
    assert state is not None
    assert state.byte_offset == path.stat().st_size


def test_codex_context_events_capture_structure_and_pair_compaction(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    secret = "do not store this verbatim"
    write_rollout(
        root,
        [
            meta(SID, base_instructions={"text": "base rules"}),
            env(
                "2026-06-09T20:00:01Z",
                "turn_context",
                {
                    "turn_id": "turn-1",
                    "developer_instructions": "developer rules",
                    "user_instructions": "user rules",
                },
            ),
            env("2026-06-09T20:00:02Z", "event_msg", {"type": "user_message", "message": secret}),
            env(
                "2026-06-09T20:00:03Z",
                "response_item",
                {"type": "reasoning", "encrypted_content": "ciphertext-is-not-sized"},
            ),
            env(
                "2026-06-09T20:00:04Z",
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 900,
                            "cached_input_tokens": 100,
                            "output_tokens": 50,
                            "reasoning_output_tokens": 25,
                        },
                        "model_context_window": 10_000,
                    },
                },
            ),
            env(
                "2026-06-09T20:00:05Z",
                "compacted",
                {
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "condensed state"}],
                        },
                        {"type": "compaction", "encrypted_content": "opaque"},
                    ]
                },
            ),
        ],
    )
    import_codex_source(repo, resolver, root, MACHINE)

    path = next(root.rglob("*.jsonl"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(env("2026-06-09T20:00:06Z", "event_msg", {"type": "context_compacted"}))
            + "\n"
        )
    import_codex_source(repo, resolver, root, MACHINE)

    rows = conn.execute(
        "SELECT * FROM context_events WHERE session_id = ? ORDER BY idx", (SID,)
    ).fetchall()
    assert [row["idx"] for row in rows] == list(range(len(rows)))
    assert sum(row["event_type"] == "compact_boundary" for row in rows) == 1
    assert sum(row["event_type"] == "compact_marker" for row in rows) == 1
    assert any(
        row["category"] == "user_prompt" and row["byte_count"] == len(secret.encode())
        for row in rows
    )
    opaque = next(row for row in rows if row["event_type"] == "opaque_reasoning")
    assert opaque["byte_count"] is None
    usage = next(row for row in rows if row["event_type"] == "model_call")
    assert usage["input_tokens"] == 900
    assert usage["context_window"] == 10_000
    replacement = next(
        row for row in rows if row["fingerprint"] == hashlib.sha256(b"condensed state").hexdigest()
    )
    assert json.loads(replacement["extra"])["replacement"] is True
    serialized = "\n".join(str(tuple(row)) for row in rows)
    assert secret not in serialized


def test_codex_exec_header_without_exec_event(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    """CLI >= ~0.129 emits no exec_command_end; the output header is the only telemetry."""
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID, source="vscode"),
            env(
                "2026-06-09T20:00:01Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call_h",
                    "arguments": json.dumps({"command": "uv run pytest"}),
                },
            ),
            env(
                "2026-06-09T20:00:04Z",
                "response_item",
                {
                    "type": "function_call_output",
                    "call_id": "call_h",
                    "output": "Exit code: 1\nWall time: 2.9 seconds\nOutput:\n1 failed, 3 passed",
                },
            ),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    row = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_h'").fetchone()
    assert row["exit_code"] == 1
    assert row["is_error"] == 1
    assert row["duration_ms"] == 2900
    assert row["output_preview"].startswith("Exit code: 1")
    session = conn.execute(
        "SELECT entrypoint FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["entrypoint"] == "vscode"


def test_codex_custom_tool_output_metadata(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    inner = {
        "output": "Success. Updated the following files:\nM src/app.py\n",
        "metadata": {"exit_code": 0, "duration_seconds": 0.4},
    }
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID),
            env(
                "2026-06-09T20:00:01Z",
                "response_item",
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "call_c",
                    "input": "*** Begin Patch",
                },
            ),
            env(
                "2026-06-09T20:00:02Z",
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_c",
                    "output": json.dumps(inner),
                },
            ),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    row = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_c'").fetchone()
    assert row["exit_code"] == 0
    assert row["is_error"] == 0
    assert row["duration_ms"] == 400
    assert row["output_preview"].startswith("Success.")


def test_codex_mcp_tool_call_end(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID),
            env(
                "2026-06-09T20:00:01Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "search_graph",
                    "call_id": "call_m1",
                    "arguments": "{}",
                },
            ),
            env(
                "2026-06-09T20:00:02Z",
                "event_msg",
                {
                    "type": "mcp_tool_call_end",
                    "call_id": "call_m1",
                    "invocation": {"server": "codebase-memory", "tool": "search_graph"},
                    "duration": {"secs": 1, "nanos": 231_293_400},
                    "result": {"Ok": {"content": [], "is_error": False}},
                },
            ),
            env(
                "2026-06-09T20:00:03Z",
                "response_item",
                {"type": "function_call", "name": "js", "call_id": "call_m2", "arguments": "{}"},
            ),
            env(
                "2026-06-09T20:00:04Z",
                "event_msg",
                {
                    "type": "mcp_tool_call_end",
                    "call_id": "call_m2",
                    "invocation": {"server": "node_repl", "tool": "js"},
                    "duration": {"secs": 0, "nanos": 5_000_000},
                    "result": {"Err": "boom"},
                },
            ),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    ok = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_m1'").fetchone()
    assert ok["category"] == "mcp"
    assert ok["duration_ms"] == 1231
    assert ok["is_error"] == 0
    assert json.loads(ok["extra"])["mcp_server"] == "codebase-memory"

    err = conn.execute("SELECT * FROM tool_calls WHERE call_id = 'call_m2'").fetchone()
    assert err["category"] == "mcp"
    assert err["is_error"] == 1


def test_codex_turn_accounting(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID),
            env("2026-06-09T20:00:01Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            env(
                "2026-06-09T20:00:02Z",
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "duration_ms": 1000,
                    "time_to_first_token_ms": 200,
                },
            ),
            env("2026-06-09T20:00:03Z", "event_msg", {"type": "task_started", "turn_id": "t2"}),
            env(
                "2026-06-09T20:00:04Z",
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "t2",
                    "duration_ms": 3000,
                    "time_to_first_token_ms": 400,
                },
            ),
            env("2026-06-09T20:00:05Z", "event_msg", {"type": "task_started", "turn_id": "t3"}),
            env(
                "2026-06-09T20:00:06Z",
                "event_msg",
                {"type": "turn_aborted", "turn_id": "t3", "reason": "interrupted"},
            ),
            env("2026-06-09T20:00:07Z", "event_msg", {"type": "thread_rolled_back"}),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (SID,)).fetchone()
    assert session["turn_count"] == 3
    assert session["task_duration_ms"] == 4000
    assert session["time_to_first_token_ms"] == 300
    assert session["turn_abort_count"] == 1

    aborted = conn.execute(
        """
        SELECT extra FROM messages
        WHERE session_id = ? AND json_extract(extra, '$.subtype') = 'turn_aborted'
        """,
        (SID,),
    ).fetchone()
    assert json.loads(aborted["extra"])["reason"] == "interrupted"
    rolled_back = conn.execute(
        """
        SELECT COUNT(*) AS n FROM messages
        WHERE session_id = ? AND json_extract(extra, '$.subtype') = 'thread_rolled_back'
        """,
        (SID,),
    ).fetchone()
    assert rolled_back["n"] == 1


def test_codex_error_info_and_plan_type(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID),
            env(
                "2026-06-09T20:00:01Z",
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5}},
                    "rate_limits": {"primary": {"used_percent": 1.0}, "plan_type": "plus"},
                },
            ),
            env(
                "2026-06-09T20:00:02Z",
                "event_msg",
                {
                    "type": "error",
                    "message": "stream disconnected before completion",
                    "codex_error_info": "other",
                },
            ),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (SID,)).fetchone()
    assert session["rate_limit_plan"] == "plus"
    assert session["api_error_count"] == 1
    error = conn.execute(
        """
        SELECT extra FROM messages
        WHERE session_id = ? AND json_extract(extra, '$.subtype') = 'api_error'
        """,
        (SID,),
    ).fetchone()
    assert json.loads(error["extra"])["error_info"] == "other"


def test_codex_web_search_counted(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID),
            env(
                "2026-06-09T20:00:01Z",
                "response_item",
                {
                    "type": "web_search_call",
                    "call_id": "call_w",
                    "action": {"query": "fastapi htmx"},
                },
            ),
        ],
    )

    import_codex_source(repo, resolver, root, MACHINE)

    session = conn.execute(
        "SELECT web_search_requests FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()
    assert session["web_search_requests"] == 1


def test_codex_subagent_child_session(
    tmp_path: Path, repo: Repository, resolver: ProjectResolver, conn: sqlite3.Connection
) -> None:
    root = tmp_path / "codex"
    write_rollout(
        root,
        [
            meta(SID, source="vscode"),
            env("2026-06-09T20:00:01Z", "event_msg", {"type": "user_message", "message": "hi"}),
        ],
    )
    write_rollout(
        root,
        [
            meta(
                CHILD_SID,
                source={"subagent": {"other": "guardian"}},
                thread_source="subagent",
                parent_thread_id=SID,
            ),
            env("2026-06-09T20:05:01Z", "turn_context", {"model": "gpt-5.4"}),
            env(
                "2026-06-09T20:05:02Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "cc1",
                    "arguments": json.dumps({"command": "ls"}),
                },
            ),
            env(
                "2026-06-09T20:05:03Z",
                "response_item",
                {
                    "type": "function_call_output",
                    "call_id": "cc1",
                    "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nok",
                },
            ),
            env(
                "2026-06-09T20:05:04Z",
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 500,
                            "cached_input_tokens": 100,
                            "output_tokens": 50,
                        }
                    },
                },
            ),
        ],
        sid=CHILD_SID,
        ts="2026-06-09T20-05-00",
    )

    import_codex_source(repo, resolver, root, MACHINE)

    child = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (CHILD_SID,)).fetchone()
    assert child["parent_session_id"] == SID
    assert child["entrypoint"] == "subagent:guardian"
    assert child["is_noise"] == 1

    run = conn.execute(
        "SELECT * FROM subagent_runs WHERE agent_run_id = ?", (CHILD_SID,)
    ).fetchone()
    assert run is not None
    assert run["session_id"] == SID
    assert run["agent_type"] == "guardian"
    assert run["total_tokens"] == 650
    assert run["total_tool_use_count"] == 1
    assert run["bash_count"] == 1
