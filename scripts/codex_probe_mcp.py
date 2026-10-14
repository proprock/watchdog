"""A tiny stdio MCP server used as a local Codex probe fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

PROTOCOL_VERSION = "2024-11-05"
SENTINEL_NAME = "wd139c_sentinel.txt"


def _reply(output: TextIO, request_id: object, result: object) -> None:
    output.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    output.flush()


def _error(output: TextIO, request_id: object, code: int, message: str) -> None:
    output.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )
        + "\n"
    )
    output.flush()


def _tool_result(output: TextIO, request_id: object, text: str, work: Path) -> None:
    try:
        (work / SENTINEL_NAME).write_text(text, encoding="utf-8")
    except (OSError, UnicodeError):
        _reply(
            output,
            request_id,
            {
                "content": [{"type": "text", "text": "Unable to write probe sentinel"}],
                "isError": True,
            },
        )
        return
    _reply(
        output,
        request_id,
        {"content": [{"type": "text", "text": text}], "isError": False},
    )


def _dispatch(request: object, output: TextIO, work: Path) -> None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        _error(output, None, -32600, "Invalid Request")
        return

    method = request.get("method")
    if not isinstance(method, str):
        _error(output, request.get("id"), -32600, "Invalid Request")
        return

    if method == "notifications/initialized" and "id" not in request:
        return

    request_id = request.get("id")
    if "id" not in request:
        # Unknown notifications are ignored, as required by JSON-RPC.
        return

    params = request.get("params", {})
    if not isinstance(params, dict):
        _error(output, request_id, -32602, "Invalid params")
        return

    if method == "initialize":
        _reply(
            output,
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "watchdog-codex-probe", "version": "1.0"},
            },
        )
    elif method == "ping":
        _reply(output, request_id, {})
    elif method == "tools/list":
        _reply(
            output,
            request_id,
            {
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
            },
        )
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments")
        if tool_name != "emit":
            _reply(
                output,
                request_id,
                {
                    "content": [{"type": "text", "text": "Unknown tool"}],
                    "isError": True,
                },
            )
        elif (
            not isinstance(arguments, dict)
            or set(arguments) != {"text"}
            or not isinstance(arguments.get("text"), str)
        ):
            _reply(
                output,
                request_id,
                {
                    "content": [{"type": "text", "text": "Invalid emit arguments"}],
                    "isError": True,
                },
            )
        else:
            _tool_result(output, request_id, arguments["text"], work)
    else:
        _error(output, request_id, -32601, "Method not found")


def serve(input_stream: TextIO, output_stream: TextIO, work: Path) -> None:
    """Serve newline-delimited JSON-RPC requests until the input stream closes."""
    for line in input_stream:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _error(output_stream, None, -32700, "Parse error")
            continue
        _dispatch(request, output_stream, work)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work", type=Path, required=True, help="Existing directory for the sentinel file"
    )
    args = parser.parse_args(argv)
    if not args.work.is_dir():
        parser.error("--work must be an existing directory")
    serve(sys.stdin, sys.stdout, args.work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
