"""Codex CLI adapter tests without external model calls."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from wdi.llm import backend


def test_codex_uses_stdin_schema_ephemeral_mode_and_final_file(monkeypatch: pytest.MonkeyPatch):
    response = {"summary": "Good", "advice": ["Batch reads"], "focus_areas": ["I/O"]}
    workdirs: list[Path] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[1] == "exec"
        assert args[-1] == "-"
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[args.index("--model") + 1] == "test-model"
        assert kwargs["input"] == "metrics payload"
        assert kwargs["encoding"] == "utf-8"
        workdirs.append(Path(kwargs["cwd"]))
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert set(schema["required"]) == {"summary", "advice", "focus_areas"}
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(response))
        return subprocess.CompletedProcess(args, 0, "event output is not the answer", "")

    monkeypatch.setattr(backend.subprocess, "run", run)
    result = backend.CodexCliBackend().complete("metrics payload", "test-model")
    assert json.loads(result) == response
    assert not workdirs[0].exists()


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "empty", "invalid"])
def test_codex_errors_are_readable_and_do_not_expose_output(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "missing":
            raise FileNotFoundError("private path")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 180)
        if failure == "invalid":
            Path(args[args.index("--output-last-message") + 1]).write_text("private bad output")
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0, "", "secret")

    monkeypatch.setattr(backend.subprocess, "run", run)
    with pytest.raises(backend.LLMError) as exc:
        backend.CodexCliBackend().complete("metrics", "test-model")
    assert "private" not in str(exc.value)
    assert "secret" not in str(exc.value)


def test_codex_upgrade_error_explains_executable_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "model requires a newer version of Codex")

    monkeypatch.setattr(backend.subprocess, "run", run)
    with pytest.raises(backend.LLMError, match="codex_executable"):
        backend.CodexCliBackend().complete("synthetic input", "test-model")
