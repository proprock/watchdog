"""LLM backend protocol and local Claude/Codex CLI implementations."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol


class LLMError(Exception):
    """Raised when the backend fails to produce a completion."""


class LLMBackend(Protocol):
    def complete(self, prompt: str, model: str) -> str:
        """Return the model's text response for the prompt."""
        ...


class CodexCliBackend:
    """Run an ephemeral Codex coaching request in an isolated working directory."""

    def __init__(self, executable: str = "codex", timeout: float = 180.0) -> None:
        self._executable = shutil.which(executable) or executable
        self._timeout = timeout

    def complete(self, prompt: str, model: str) -> str:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string"},
                "advice": {"type": "array", "items": {"type": "string"}},
                "focus_areas": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "advice", "focus_areas"],
        }
        with TemporaryDirectory(prefix="wdi-coach-") as directory:
            root = Path(directory)
            schema_path = root / "schema.json"
            output_path = root / "answer.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [
                        self._executable,
                        "exec",
                        "--ephemeral",
                        "--ignore-user-config",
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        "--model",
                        model,
                        "--output-schema",
                        str(schema_path),
                        "--output-last-message",
                        str(output_path),
                        "-",
                    ],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=self._timeout,
                    cwd=root,
                )
            except FileNotFoundError as exc:
                raise LLMError("Codex CLI is not installed or is not on PATH.") from exc
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"Codex CLI timed out after {self._timeout}s.") from exc
            if proc.returncode != 0:
                if "requires a newer version of Codex" in proc.stderr:
                    raise LLMError(
                        "This model requires a newer Codex CLI. Update Codex or set "
                        "features.llm_analysis.codex_executable to a newer executable."
                    )
                raise LLMError(
                    f"Codex CLI exited with code {proc.returncode}. "
                    "Check CLI sign-in and model access."
                )
            try:
                text = output_path.read_text(encoding="utf-8")
                data = json.loads(text)
            except (OSError, ValueError) as exc:
                raise LLMError("Codex CLI did not return a valid coaching response.") from exc
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("summary"), str)
                or any(
                    not isinstance(data.get(key), list)
                    or any(not isinstance(item, str) for item in data[key])
                    for key in ("advice", "focus_areas")
                )
            ):
                raise LLMError("Codex CLI returned an unexpected coaching response format.")
            return text


class ClaudeCliBackend:
    """Call the locally installed Claude Code CLI in headless mode.

    Uses `claude -p --output-format json --model <model>` and feeds the prompt on
    stdin (not as an argv, which would hit the Windows command-line length limit).
    Returns the `result` text. No API key is required; auth comes from the CLI.
    """

    def __init__(self, executable: str = "claude", timeout: float = 180.0) -> None:
        self._executable = shutil.which(executable) or executable
        self._timeout = timeout

    def complete(self, prompt: str, model: str) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 - executable resolved via PATH, args fixed
                [
                    self._executable,
                    "-p",
                    "--output-format",
                    "json",
                    "--model",
                    model,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise LLMError(f"claude CLI not found: {self._executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self._timeout}s") from exc

        if proc.returncode != 0:
            raise LLMError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError("claude CLI returned non-JSON output") from exc
        if payload.get("is_error"):
            raise LLMError(f"claude CLI reported an error: {payload.get('subtype')}")
        result = payload.get("result")
        if not isinstance(result, str):
            raise LLMError("claude CLI response had no 'result' text")
        return result
