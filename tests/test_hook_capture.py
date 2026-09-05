import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_hook.py"


def run_capture(directory, payload, *options):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(directory), *options],
        input=payload,
        text=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize("payload", ["{", "[]", "null"])
def test_invalid_input_is_fail_open(tmp_path, payload):
    result = run_capture(tmp_path, payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert not list(tmp_path.glob("*.json"))


def test_capture_keeps_structure_and_correlation_without_content(tmp_path):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "private-session",
        "cwd": "private-path",
        "tool_input": {"command": "private-command"},
        "tool_response": {"exit_code": 1, "stdout": "private-output"},
        "arbitrary-secret-key": "private-value",
    }
    for _ in range(2):
        assert run_capture(tmp_path, json.dumps(payload)).returncode == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 2
    records = [json.loads(path.read_text()) for path in files]
    assert records[0]["identities"]["session_id"] == records[1]["identities"]["session_id"]
    assert records[0]["tool_response_fields"] == {"exit_code": "int", "stdout": "str"}
    assert records[0]["exit_code"] == 1
    assert all("private-" not in path.read_text() for path in files)
    assert all("arbitrary-secret" not in path.read_text() for path in files)


def test_codex_noop_response_and_unwritable_target(tmp_path):
    target = tmp_path / "file"
    target.write_text("occupied")
    result = run_capture(target, '{"hook_event_name":"Stop"}', "--json-noop")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
