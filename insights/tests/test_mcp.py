"""Tests for the MCP server (`wdi mcp`) tools."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from wdi.config import Settings
from wdi.db.connection import connect
from wdi.db.repository import Repository
from wdi.mcp import create_mcp_server

MACHINE = "m1"
TOOL_NAMES = {
    "get_recent_sessions",
    "get_tool_failure_patterns",
    "get_oversearching_examples",
    "get_prompt_quality_report",
}


def _seed(conn: sqlite3.Connection) -> None:
    repo = Repository(conn)
    repo.ensure_machine(MACHINE)
    project_id = repo.get_or_create_project("demo")
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, machine_id, project_id, agent, model, title, started_at,
             input_tokens, output_tokens, tool_call_count)
        VALUES ('s1', ?, ?, 'claude', 'claude-opus-4-8', 'Demo session',
                '2026-06-01T10:00:00Z', 1000, 500, 7)
        """,
        (MACHINE, project_id),
    )
    # search-heavy / edit-light tool use, a repeated read of the same target,
    # and one failing Bash call.
    rows = [
        ("Read", "read", "a.py", 0),
        ("Read", "read", "a.py", 0),  # repeated read of a.py -> repeated_reads = 1
        ("Grep", "search", None, 0),
        ("Grep", "search", None, 0),
        ("Grep", "search", None, 0),
        ("Edit", "edit", "a.py", 0),
        ("Bash", "exec", None, 1),  # failing tool
    ]
    conn.executemany(
        """
        INSERT INTO tool_calls
            (session_id, machine_id, stream_id, idx, tool_name, category, target, is_error)
        VALUES ('s1', ?, 'main', ?, ?, ?, ?, ?)
        """,
        [(MACHINE, i, name, cat, tgt, err) for i, (name, cat, tgt, err) in enumerate(rows)],
    )
    repo.upsert_llm_analysis(
        "s1",
        MACHINE,
        analysis_type="prompt_quality",
        prompt_version="v1",
        model="claude-opus-4-8",
        score=3.5,
        result={"score": 3.5, "summary": "vague prompts"},
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "wdi.sqlite"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(machine_id=MACHINE, db_path=db_path)


@pytest.fixture
def seeded(db_path: Path) -> Iterator[None]:
    conn = connect(db_path)
    _seed(conn)
    conn.close()
    yield


@pytest.fixture
def empty_db(db_path: Path) -> Iterator[None]:
    conn = connect(db_path)
    conn.close()
    yield


def _tool(server: FastMCP, name: str) -> Callable[..., list[dict[str, object]]]:
    tool = server._tool_manager.get_tool(name)
    assert tool is not None
    return tool.fn


def test_registers_exactly_the_four_named_tools(settings: Settings) -> None:
    server = create_mcp_server(settings)
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == TOOL_NAMES


def test_recent_sessions_initializes_new_database(settings: Settings) -> None:
    assert _tool(create_mcp_server(settings), "get_recent_sessions")() == []


def test_recent_sessions(settings: Settings, seeded: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_recent_sessions")()
    assert [r["session_id"] for r in rows] == ["s1"]
    assert rows[0]["title"] == "Demo session"


def test_tool_failure_patterns(settings: Settings, seeded: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_tool_failure_patterns")()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "Bash"
    assert rows[0]["calls"] == 1
    assert rows[0]["errors"] == 1
    assert rows[0]["error_rate"] == 1.0


def test_oversearching_examples(settings: Settings, seeded: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_oversearching_examples")()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["search_count"] == 3
    assert row["edit_count"] == 1
    assert row["repeated_reads"] == 1


def test_prompt_quality_report_parses_result(settings: Settings, seeded: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_prompt_quality_report")()
    assert len(rows) == 1
    assert rows[0]["score"] == 3.5
    assert rows[0]["result"] == {"score": 3.5, "summary": "vague prompts"}


def test_prompt_quality_empty_when_unanalyzed(settings: Settings, empty_db: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_prompt_quality_report")()
    assert rows == []


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_tools_handle_empty_db(settings: Settings, empty_db: None, name: str) -> None:
    assert _tool(create_mcp_server(settings), name)() == []


def test_agent_filter_excludes_other_agents(settings: Settings, seeded: None) -> None:
    rows = _tool(create_mcp_server(settings), "get_recent_sessions")(agent="codex")
    assert rows == []
