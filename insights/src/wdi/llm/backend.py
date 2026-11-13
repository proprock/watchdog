"""LLM backend protocol and local Claude/Codex CLI implementations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from agent_watchdog import readapi
from agent_watchdog.insights import llm


class LLMError(Exception):
    """Raised when the backend fails to produce a completion."""


class LLMBackend(Protocol):
    def complete(self, prompt: str, model: str) -> str:
        """Return the model's text response for the prompt."""
        ...


class CodexCliBackend:
    """Run an ephemeral Codex request in a temporary working directory."""

    def __init__(
        self, executable: str = "codex", timeout: float = 180.0, *, structured: bool = True
    ) -> None:
        native = shutil.which("codex.exe") if os.name == "nt" and executable == "codex" else None
        self._executable = native or shutil.which(executable) or executable
        self._timeout = timeout
        self._structured = structured

    def complete(self, prompt: str, model: str) -> str:
        if os.name == "nt" and Path(self._executable).suffix.lower() in {".bat", ".cmd"}:
            raise LLMError(
                "Set features.llm_analysis.codex_executable to a native Codex executable."
            )
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
        with TemporaryDirectory(prefix="wdi-codex-") as directory:
            root = Path(directory)
            output_path = root / "answer.json"
            schema_args = []
            if self._structured:
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                schema_args = ["--output-schema", str(schema_path)]
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
                        *schema_args,
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
                if not self._structured:
                    if not text.strip():
                        raise ValueError("empty output")
                    return text
                data = json.loads(text)
            except (OSError, ValueError) as exc:
                raise LLMError("Codex CLI did not return a valid response.") from exc
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
    """Complete a text prompt using Watchdog's isolated Claude runner."""

    def __init__(
        self,
        executable: str = "claude",
        timeout: float = 180.0,
        *,
        data_dirs: Sequence[Path] | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        default = readapi.default_data_dir()
        self._data_dirs = tuple(dict.fromkeys((default, *(data_dirs or ()))))

    def complete(self, prompt: str, model: str) -> str:
        try:
            roots = [
                Path(store.root)
                for directory in self._data_dirs
                for store in readapi.stores(directory)
                if store.root is not None
            ]
        except (OSError, ValueError) as exc:
            raise LLMError(
                f"isolation_unavailable: cannot read Watchdog store manifests ({exc})"
            ) from exc
        result = llm.claude(
            llm.Request("", prompt, None, model, None, self._timeout),
            forbidden_roots=roots,
            executable=self._executable,
        )
        if result.reason:
            raise LLMError(result.reason)
        text = (result.output or {}).get("text")
        if not isinstance(text, str):
            raise LLMError("malformed_output")
        return text
