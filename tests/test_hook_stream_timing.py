import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def mod():
    path = Path(__file__).parents[1] / "scripts" / "hook_stream_timing.py"
    spec = importlib.util.spec_from_file_location("hook_stream_timing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAMP = "readMonotonicNs"


def stamped(subtype, hook_id, event, ns, **extra):
    return {
        STAMP: ns,
        "type": "hook",
        "subtype": subtype,
        "hook_id": hook_id,
        "hook_name": f"{event}:Bash",
        "hook_event": event,
        "outcome": extra.get("outcome", "success"),
        "exit_code": extra.get("exit_code", 0),
        "stdout_len": extra.get("stdout_len", 0),
        "session_id": "s1",
    }


def test_report_pairs_by_hook_id_and_computes_deltas(mod, tmp_path):
    rows = [
        stamped("hook_started", "h1", "PreToolUse", 1_000_000_000),
        stamped("hook_response", "h1", "PreToolUse", 1_040_000_000),
        stamped("hook_started", "h2", "PostToolUse", 2_000_000_000),
        stamped("hook_response", "h2", "PostToolUse", 2_210_000_000, stdout_len=3),
        stamped("hook_started", "h3", "Stop", 3_000_000_000),  # unpaired
        {"type": "tool_use_id", "id": "toolu_x"},
        {"type": "tool_use_id", "id": "toolu_y"},
        {"type": "run", "session_id": "s1", "num_turns": 3, "is_error": False},
    ]
    stream = tmp_path / "run1.ndjson"
    stream.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "timing.json"
    rc = mod.report(SimpleNamespace(stream=[stream], output=out))
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["hook_pairs"] == 2
    assert result["unpaired_hook_started"] == 1
    assert result["delta_ms"] == {"count": 2, "p50_ms": 40.0, "p95_ms": 210.0, "max_ms": 210.0}
    assert result["per_event"] == {"PostToolUse": 1, "PreToolUse": 1}
    assert result["outcomes"] == {"success": 2}
    assert result["nonempty_stdout_responses"] == 1
    assert result["tool_use_ids"] == ["toolu_x", "toolu_y"]
    assert result["runs"] == 1 and result["errored_runs"] == 0


def test_capture_writes_stamped_hook_rows_without_content(mod, tmp_path, monkeypatch):
    stream_lines = [
        json.dumps({"type": "system", "subtype": "init", "cwd": "/secret/repo"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "toolu_a", "name": "Bash"}]},
            }
        ),
        json.dumps(
            {
                "type": "system",
                "subtype": "hook_started",
                "hook_id": "H1",
                "hook_name": "PreToolUse:Bash",
                "hook_event": "PreToolUse",
                "session_id": "sid",
            }
        ),
        json.dumps(
            {
                "type": "system",
                "subtype": "hook_response",
                "hook_id": "H1",
                "hook_name": "PreToolUse:Bash",
                "hook_event": "PreToolUse",
                "stdout": "",
                "exit_code": 0,
                "outcome": "success",
                "session_id": "sid",
            }
        ),
        json.dumps({"type": "result", "session_id": "sid", "num_turns": 2, "is_error": False}),
    ]

    class FakeProc:
        def __init__(self):
            self.stdout = iter(line + "\n" for line in stream_lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        assert "--include-hook-events" in command and "-p" in command
        return FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    out = tmp_path / "cap.ndjson"
    rc = mod.capture(
        SimpleNamespace(
            claude_bin="claude",
            session_id="sid",
            cwd=str(tmp_path),
            settings=str(tmp_path / "settings.json"),
            setting_sources="",
            add_dir=[],
            prompt="secret prompt text",
            out=out,
        )
    )
    assert rc == 0
    body = out.read_text()
    assert "secret prompt text" not in body and "/secret/repo" not in body
    lines = [json.loads(x) for x in body.splitlines()]
    hooks = [line for line in lines if line["type"] == "hook"]
    assert {h["subtype"] for h in hooks} == {"hook_started", "hook_response"}
    assert all(STAMP in h for h in hooks)
    assert any(line["type"] == "tool_use_id" and line["id"] == "toolu_a" for line in lines)
    assert lines[-1]["type"] == "run" and lines[-1]["num_turns"] == 2
