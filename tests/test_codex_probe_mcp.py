"""Offline contract tests for the local Codex MCP probe fixture."""

import importlib.util
import io
import json
import sys
from pathlib import Path


def _probe_server():
    path = Path(__file__).parents[1] / "scripts" / "codex_probe_mcp.py"
    spec = importlib.util.spec_from_file_location("codex_probe_mcp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe_server = _probe_server()


def _run(requests: list[dict[str, object]], work: Path) -> list[dict[str, object]]:
    input_stream = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    output_stream = io.StringIO()
    probe_server.serve(input_stream, output_stream, work)
    return [json.loads(line) for line in output_stream.getvalue().splitlines()]


def _result(reply: dict[str, object]) -> dict[str, object]:
    result = reply.get("result")
    assert isinstance(result, dict)
    return result


def _content_text(result: dict[str, object]) -> str:
    content = result.get("content")
    assert isinstance(content, list) and content
    first = content[0]
    assert isinstance(first, dict)
    text = first.get("text")
    assert isinstance(text, str)
    return text


def test_initialize_ping_and_initialized_notification(tmp_path):
    replies = _run(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ],
        tmp_path,
    )

    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "watchdog-codex-probe", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "result": {}},
    ]


def test_tools_list_exposes_only_emit_with_text_schema(tmp_path):
    replies = _run([{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}], tmp_path)

    assert replies[0]["result"] == {
        "tools": [
            {
                "name": "emit",
                "description": "Write text to the probe sentinel file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
    }


def test_emit_writes_only_the_fixed_sentinel_and_returns_text(tmp_path):
    replies = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "emit", "arguments": {"text": "probe effect"}},
            }
        ],
        tmp_path,
    )

    assert (tmp_path / "wd139c_sentinel.txt").read_text(encoding="utf-8") == "probe effect"
    assert list(path.name for path in tmp_path.iterdir()) == ["wd139c_sentinel.txt"]
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {
                "content": [{"type": "text", "text": "probe effect"}],
                "isError": False,
            },
        }
    ]


def test_invalid_arguments_tool_and_method_fail_without_writing(tmp_path):
    replies = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "emit", "arguments": {"text": 42}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "other", "arguments": {"text": "blocked"}},
            },
            {"jsonrpc": "2.0", "id": 7, "method": "not/a-method"},
        ],
        tmp_path,
    )

    invalid_arguments = _result(replies[0])
    assert invalid_arguments.get("isError") is True
    assert _content_text(invalid_arguments) == "Invalid emit arguments"
    unknown_tool = _result(replies[1])
    assert unknown_tool.get("isError") is True
    assert _content_text(unknown_tool) == "Unknown tool"
    assert replies[2]["error"] == {"code": -32601, "message": "Method not found"}
    assert list(tmp_path.iterdir()) == []


def test_emit_write_failure_is_returned_as_a_tool_error(tmp_path):
    (tmp_path / "wd139c_sentinel.txt").mkdir()

    replies = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "emit", "arguments": {"text": "cannot write"}},
            }
        ],
        tmp_path,
    )

    result = _result(replies[0])
    assert result.get("isError") is True
    assert _content_text(result) == "Unable to write probe sentinel"
