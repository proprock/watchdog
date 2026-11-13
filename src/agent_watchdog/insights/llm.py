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
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Any

from agent_watchdog import _proc, privacy

PREFLIGHT_TIMEOUT = 30.0
_claude_preflight_lock = Lock()


@dataclass(frozen=True)
class Request:
    system_prompt: str
    prompt: str
    schema: dict[str, Any] | None
    model: str
    effort: str | None
    timeout: float
    backend: str = "claude"


@dataclass(frozen=True)
class Result:
    """``reason`` is None on success; otherwise ``output`` is None."""

    output: dict[str, Any] | None
    reason: str | None
    provenance: dict[str, Any] = field(default_factory=dict)


Runner = Callable[[Request], Result]


def runner(backend: str, *, forbidden_roots: Sequence[Path]) -> Runner:
    """Select analysis independently of the observed provider, without fallback."""
    implementations = {"claude": claude, "codex": codex}
    if backend not in implementations:
        raise ValueError(f"Unknown analysis backend: {backend}")
    roots = tuple(forbidden_roots)

    def selected(request: Request) -> Result:
        if request.backend != backend:
            return Result(None, "backend_mismatch", {"provider": backend})
        return implementations[backend](request, forbidden_roots=roots)

    return selected


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
    if request.schema is not None:
        argv += ["--json-schema", json.dumps(request.schema)]
    if request.system_prompt:
        argv += ["--system-prompt", request.system_prompt]
    return argv


def _shown(argv: list[str]) -> list[str]:
    """Record the flags without the long system prompt and schema values."""
    return [
        "<omitted>" if index and argv[index - 1] in {"--json-schema", "--system-prompt"} else arg
        for index, arg in enumerate(argv)
    ]


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            if path.is_relative_to(root.resolve()):
                return True
        except OSError:
            continue
    return False


def _answer(envelope: dict[str, Any], *, structured_output: bool) -> dict[str, Any] | None:
    text = envelope.get("result")
    if not structured_output:
        return {"text": text} if isinstance(text, str) else None
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
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


@cache
def _claude_preflight(executable: str, mtime_ns: int) -> tuple[str | None, str | None]:
    try:
        version = _run([executable, "--version"], timeout=PREFLIGHT_TIMEOUT)
        help_text = _run([executable, "--help"], timeout=PREFLIGHT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "not_found"
    first_line = version.stdout.partition("\n")[0].strip()
    safe_mode = (
        version.returncode == help_text.returncode == 0 and "--safe-mode" in help_text.stdout
    )
    return first_line or None, None if safe_mode else "isolation_unavailable"


def claude(
    request: Request, *, forbidden_roots: Sequence[Path], executable: str = "claude"
) -> Result:
    """Run one isolated analysis call; every failure maps to a reason, never an exception."""
    resolved = shutil.which(executable)
    resolved = str(Path(resolved).absolute()) if resolved is not None else None
    provenance: dict[str, Any] = {"provider": "claude", "executable": resolved}
    if resolved is None:
        return Result(None, "not_found", provenance)
    # Windows may run batch launchers through cmd.exe even with shell=False.
    if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
        return Result(None, "isolation_unavailable", provenance)
    workdir: Path | None = None
    try:
        # Hold the lock through a cache miss so parallel workers share the preflight.
        with _claude_preflight_lock:
            version, reason = _claude_preflight(resolved, Path(resolved).stat().st_mtime_ns)
        provenance["version"] = version
        if reason is not None:
            return Result(None, reason, provenance)
        try:
            workdir = Path(tempfile.mkdtemp(prefix="agent-watchdog-insights-"))
            resolved_workdir = workdir.resolve()
            inside_project = any(
                resolved_workdir.is_relative_to(root.resolve()) for root in forbidden_roots
            )
        except OSError, ValueError:
            return Result(None, "isolation_unavailable", provenance)
        if inside_project:
            return Result(None, "isolation_unavailable", provenance)
        argv = _argv(resolved, request)
        provenance |= {"argv": _shown(argv), "requested_model": request.model}
        started = time.monotonic()
        completed = _run(argv, timeout=request.timeout, input=request.prompt, cwd=resolved_workdir)
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
    answer = _answer(envelope, structured_output=request.schema is not None)
    if answer is None:
        return Result(None, "malformed_output", provenance)
    return Result(answer, None, provenance)


def codex(request: Request, *, forbidden_roots: Sequence[Path]) -> Result:
    """Preflight Codex, refusing analysis until an isolation contract is established.

    The inspected CLI has no proven equivalent of Claude's safe mode plus empty
    tool set. Ignoring config, ephemeral sessions and a read-only sandbox do not
    establish absence of instructions, skills, plugins, MCP, hooks and tools.
    Never send the evidence or launch a session on this unverified path (WD-166).
    """
    executable = shutil.which("codex")
    provenance: dict[str, Any] = {
        "provider": "codex",
        "executable": executable,
        "requested_model": request.model,
        "requested_effort": request.effort,
        "usage": None,
        "cost_usd": None,
        "context_window": None,
        "max_output_tokens": None,
        "isolation": {"verified": False, "reason": "no_verified_isolation_contract"},
    }
    if executable is None:
        return Result(None, "not_found", provenance)
    workdir: Path | None = None
    started = time.monotonic()
    try:
        workdir = Path(tempfile.mkdtemp(prefix="agent-watchdog-analysis-preflight-")).resolve()
        if _inside(workdir, forbidden_roots):
            provenance["isolation"]["reason"] = "scratch_inside_project"
            return Result(None, "isolation_unavailable", provenance)
        # Only informational commands run here. No authentication is copied or
        # read and no model command is constructed, even for a newer CLI version.
        deadline = started + min(PREFLIGHT_TIMEOUT, request.timeout)
        for arguments in (["--version"], ["exec", "--help"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Result(None, "timeout", provenance)
            completed = _run(
                [executable, *arguments],
                timeout=remaining,
                stdin=subprocess.DEVNULL,
                cwd=workdir,
            )
            if completed.returncode != 0:
                provenance["exit_code"] = completed.returncode
                return Result(None, "isolation_unavailable", provenance)
            if arguments == ["--version"]:
                version = re.fullmatch(
                    r"codex-cli (\d+\.\d+\.\d+(?:[-+][\w.+-]+)?)", completed.stdout.strip()
                )
                if version is None:
                    return Result(None, "isolation_unavailable", provenance)
                provenance["version"] = version[1]
            else:
                provenance["preflight_flags"] = [
                    flag
                    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral")
                    if flag in completed.stdout
                ]
        return Result(None, "isolation_unavailable", provenance)
    except subprocess.TimeoutExpired:
        return Result(None, "timeout", provenance)
    except OSError:
        return Result(None, "not_found", provenance)
    finally:
        provenance["wall_ms"] = round((time.monotonic() - started) * 1000)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
