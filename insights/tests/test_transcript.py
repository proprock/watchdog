"""Tests for the read-only Claude transcript segmenter."""

from __future__ import annotations

import json
from pathlib import Path

from wdi.llm import transcript
from wdi.llm.transcript import (
    ASSISTANT_TEXT,
    REASONING,
    TOOL_RESULT,
    TOOL_USE,
    USER_PROMPT,
)


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8", newline="\n"
    )


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    _write(
        path,
        [
            {"type": "user", "message": {"role": "user", "content": "Implement the parser"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I should read the file first"},
                        {"type": "text", "text": "Reading now."},
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "is_error": False,
                            "content": "FULL FILE CONTENTS " + "x" * 1000,
                        }
                    ],
                },
            },
        ],
    )
    return path


def test_iter_segments_classifies_full_fidelity(tmp_path: Path) -> None:
    with _transcript(tmp_path).open("rb") as lines:
        segs = list(transcript.iter_segments(lines))
    kinds = [s.kind for s in segs]
    assert kinds == [USER_PROMPT, REASONING, ASSISTANT_TEXT, TOOL_USE, TOOL_RESULT]

    prompt = next(s for s in segs if s.kind == USER_PROMPT)
    assert prompt.text == "Implement the parser"

    use = next(s for s in segs if s.kind == TOOL_USE)
    assert use.tool_name == "Read"
    assert use.call_id == "call-1"
    assert use.target == "a.py"

    result = next(s for s in segs if s.kind == TOOL_RESULT)
    # Full output is preserved (no 500-char preview truncation).
    assert len(result.text) > 900
    assert result.text.startswith("FULL FILE CONTENTS")


def test_find_tool_io(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    full_input, full_output = transcript.find_tool_io(path, "main", "call-1")
    assert full_input is not None and "a.py" in full_input
    assert full_output is not None and len(full_output) > 900


def test_find_tool_io_missing_source(tmp_path: Path) -> None:
    assert transcript.find_tool_io(tmp_path / "absent.jsonl", "main", "x") == (None, None)


def test_stream_source_path_resolves_subagent(tmp_path: Path) -> None:
    main = tmp_path / "sess.jsonl"
    main.write_text("{}\n", encoding="utf-8")
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    sub_file = sub_dir / "agent-deadbeef.jsonl"
    sub_file.write_text("{}\n", encoding="utf-8")

    assert transcript.stream_source_path(main, "main") == main
    assert transcript.stream_source_path(main, "deadbeef") == sub_file
    assert transcript.stream_source_path(main, "missing") is None


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        'not json\n{"type": "user", "message": "bad"}\n'
        '{"type": "user", "message": {"content": "hi"}}\n',
        encoding="utf-8",
    )
    with path.open("rb") as lines:
        segs = list(transcript.iter_segments(lines))
    assert [s.kind for s in segs] == [USER_PROMPT]
    assert segs[0].text == "hi"


def test_codex_custom_tool_result_is_read_in_full() -> None:
    output = json.dumps({"output": "result " + "x" * 900, "metadata": {"exit_code": 0}})
    record = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "call-1", "output": output},
    }

    segments = list(transcript.iter_segments([json.dumps(record)]))
    assert [(segment.call_id, segment.text) for segment in segments] == [("call-1", output)]
