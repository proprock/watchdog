"""One isolated, headless `claude -p` call per insights run (WD-014 execution mechanism).

Isolation comes from flags, not a throwaway config home, because the user's
subscription login lives in that home: `--safe-mode` disables hooks, CLAUDE.md,
skills, plugins and MCP servers while keeping authentication; `--setting-sources ""`
and `--strict-mcp-config` drop the remaining settings and servers; `--tools ""`
leaves the model nothing to execute. The call runs from a scratch directory outside
every registered project, so even a hook that fired could not resolve a project.
Without `--safe-mode` the call is refused rather than run un-isolated. Failures are
reported, never retried.
"""

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_watchdog import _proc, privacy

PREFLIGHT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Request:
    system_prompt: str
    prompt: str
    schema: dict[str, Any]
    model: str
    effort: str | None
    timeout: float


@dataclass(frozen=True)
class Result:
    """``reason`` is None on success; otherwise ``output`` is None."""

    output: dict[str, Any] | None
    reason: str | None
    provenance: dict[str, Any] = field(default_factory=dict)


Runner = Callable[[Request], Result]


def _argv(executable: str, request: Request) -> list[str]:
    argv = [
        executable,
        "-p",
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        request.model,
    ]
    if request.effort is not None:
        argv += ["--effort", request.effort]
    return argv + [
        "--json-schema",
        json.dumps(request.schema),
        "--system-prompt",
        request.system_prompt,
    ]


def _shown(argv: list[str]) -> list[str]:
    """Record the flags without the long system prompt and schema values."""
    shown = list(argv)
    for flag in ("--json-schema", "--system-prompt"):
        shown[shown.index(flag) + 1] = "<omitted>"
    return shown


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            if path.is_relative_to(root.resolve()):
                return True
        except OSError:
            continue
    return False


def _answer(envelope: dict[str, Any]) -> dict[str, Any] | None:
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = envelope.get("result")
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _limits(models: dict[str, Any], key: str) -> list[int]:
    return [
        entry[key]
        for entry in models.values()
        if isinstance(entry, dict) and type(entry.get(key)) is int
    ]


def _envelope_provenance(envelope: dict[str, Any]) -> dict[str, Any]:
    models = envelope.get("modelUsage")
    models = models if isinstance(models, dict) else {}
    windows = _limits(models, "contextWindow")
    answers = _limits(models, "maxOutputTokens")
    bases = {entry.get("costBasis") for entry in models.values() if isinstance(entry, dict)} - {
        None
    }
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "models": sorted(models),
        # A call can touch several models; the smallest limits bind the next bundle.
        "context_window": min(windows) if windows else None,
        "max_output_tokens": max(answers) if answers else None,
        "usage": {
            key: usage[key]
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
            if isinstance(usage.get(key), int)
        },
        "cost_usd": envelope.get("total_cost_usd"),
        "cost_basis": sorted(bases)[0] if len(bases) == 1 else None,
        "duration_ms": envelope.get("duration_ms"),
        "num_turns": envelope.get("num_turns"),
        "subtype": envelope.get("subtype"),
    }


def _failure_detail(stdout: str | None) -> dict[str, Any]:
    """The CLI reports API errors and limits in its JSON result, not on stderr."""
    try:
        envelope = json.loads(stdout or "")
    except json.JSONDecodeError:
        text = (stdout or "").strip()
        return {"stdout": privacy.text(text[-500:])} if text else {}
    if not isinstance(envelope, dict):
        return {}
    detail = {
        key: envelope[key]
        for key in ("subtype", "api_error_status", "terminal_reason")
        if isinstance(envelope.get(key), str | int)
    }
    if isinstance(envelope.get("result"), str):
        detail["result"] = privacy.text(envelope["result"][:500])
    return detail


def _run(argv: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return _proc.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,
    )


def claude(request: Request, *, forbidden_roots: Sequence[Path]) -> Result:
    """Run one isolated analysis call; every failure maps to a reason, never an exception."""
    executable = shutil.which("claude")
    provenance: dict[str, Any] = {"provider": "claude", "executable": executable}
    if executable is None:
        return Result(None, "not_found", provenance)
    workdir: Path | None = None
    try:
        lines = (_run([executable, "--version"], timeout=PREFLIGHT_TIMEOUT).stdout or "").split(
            "\n"
        )
        provenance["version"] = lines[0].strip() or None
        if "--safe-mode" not in (
            _run([executable, "--help"], timeout=PREFLIGHT_TIMEOUT).stdout or ""
        ):
            return Result(None, "isolation_unavailable", provenance)
        workdir = Path(tempfile.mkdtemp(prefix="agent-watchdog-insights-")).resolve()
        if _inside(workdir, forbidden_roots):
            return Result(None, "isolation_unavailable", provenance)
        argv = _argv(executable, request)
        provenance |= {"argv": _shown(argv), "requested_model": request.model}
        started = time.monotonic()
        completed = _run(argv, timeout=request.timeout, input=request.prompt, cwd=workdir)
        provenance["wall_ms"] = round((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired:
        return Result(None, "timeout", provenance)
    except OSError:
        return Result(None, "not_found", provenance)
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    if completed.returncode != 0:
        provenance["exit_code"] = completed.returncode
        provenance["stderr"] = privacy.text((completed.stderr or "").strip()[-500:])
        provenance |= _failure_detail(completed.stdout)
        return Result(None, "nonzero_exit", provenance)
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return Result(None, "malformed_output", provenance)
    if not isinstance(envelope, dict):
        return Result(None, "malformed_output", provenance)
    provenance |= _envelope_provenance(envelope)
    if envelope.get("is_error") is True:
        return Result(None, "is_error", provenance)
    answer = _answer(envelope)
    if answer is None:
        return Result(None, "malformed_output", provenance)
    return Result(answer, None, provenance)
