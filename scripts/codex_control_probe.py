"""Opt-in WD-163 probes in an isolated Codex home, with owned-process cleanup.

Run with `uv run python scripts/codex_control_probe.py --live --case NAME`.
Interactive cases additionally require --interactive and a real terminal.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import codex_production_probe as production
import live_probe as probes
from codex_hook_canary import OwnedProcessJob

from agent_watchdog.daemon import _policy_server, _SessionTracker
from agent_watchdog.storage import writer_lock

INTERACTIVE_CASES = {
    "permission-bash-allow",
    "permission-bash-deny",
    "pre-tool-bash-ask",
    "pre-compact-context",
    "post-compact-context",
    "interrupt-observe",
}

# The launcher cannot start Codex until it belongs to the owning job. Assigning
# an already-running Codex process would leave a race with its first children.
LAUNCHER = """import json, subprocess, sys, time
from pathlib import Path
gate, command = map(Path, sys.argv[1:3])
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(124)
    time.sleep(0.01)
raise SystemExit(subprocess.call(json.loads(command.read_text(encoding='utf-8')),
                                creationflags=int(sys.argv[3]),
                                stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr))
"""


def isolated_environment(home: Path, source: Mapping[str, str]) -> dict[str, str]:
    """Drop parent-session bridges; authentication is copied separately, never logged."""
    env = {key: value for key, value in source.items() if not key.startswith("CODEX_")}
    env["CODEX_HOME"] = str(home)
    return env


def observed_text(rollout: str) -> str:
    """Read model replies/tool results, excluding hook payloads and model inputs."""
    observed: list[str] = []
    for line in rollout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload", {})
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            observed.extend(
                item["text"]
                for item in payload.get("content", [])
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        elif payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            if isinstance(output, str):
                observed.append(output)
            elif isinstance(output, list):
                observed.extend(
                    item["text"]
                    for item in output
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
    return "\n".join(observed)


def command_line(codex: str, case: probes.CodexCase, work: Path, *, interactive: bool) -> list[str]:
    if case.name in INTERACTIVE_CASES and not interactive:
        raise ValueError(f"{case.name} requires an interactive terminal")
    command = [codex]
    command += ["--no-alt-screen", "--ask-for-approval", "on-request"] if interactive else ["exec"]
    permissions = ":workspace" if case.sandbox == "workspace-write" else ":read-only"
    command += [
        "--enable",
        "hooks",
        "--dangerously-bypass-hook-trust",
        "--config",
        f'default_permissions="{permissions}"',
        "-C",
        str(work),
    ]
    if case.code_mode:
        command += ["--enable", "code_mode", "--enable", "code_mode_only"]
    prompt = case.prompts[0]
    prompt = prompt.replace(
        "Bash command", "shell command using the default Windows PowerShell shell"
    )
    if case.name in {"pre-compact-context", "post-compact-context"}:
        prompt = "Reply with exactly READY."
    if case.name.startswith("permission-bash-"):
        prompt = (
            f"Use the shell tool once to run {probes._CODEX_BASH_SIDE_EFFECT}. "
            "This is an isolated permission-flow test: "
            "request sandbox_permissions=require_escalated "
            "with justification 'WD163 isolated permission probe'. Do not use apply_patch. "
            "Quote any refusal and do not retry."
        )
    if case.name == "subagent-start-context":
        prompt = (
            "Start exactly one subagent with this prompt: "
            + probes._CODEX_TOKEN_ASK
            + " Relay the subagent's exact reply. Do not invent another token."
        )
    if case.name == "interrupt-observe":
        prompt = (
            "Run Start-Sleep -Seconds 60 using the PowerShell shell tool with "
            "sandbox_permissions=require_escalated and justification 'WD163 interruption probe'. "
            "Do not spawn agents. The operator will interrupt this harmless command."
        )
    return [*command, prompt]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong),
        ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", ctypes.c_uint32),
        ("total", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
        ("terminated", ctypes.c_uint32),
    ]


def stop_job(job: OwnedProcessJob, process: subprocess.Popen) -> bool:
    """Confirm zero active processes in the owned Windows job before releasing it."""
    kernel = job._kernel32
    handle = job._handle
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.TerminateJobObject.restype = ctypes.c_int
    kernel.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = ctypes.c_int
    try:
        if not kernel.TerminateJobObject(handle, 0):
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            accounting = _Accounting()
            if not kernel.QueryInformationJobObject(
                handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                return False
            if accounting.active == 0:
                process.wait(timeout=5)
                return True
            time.sleep(0.05)
        return False
    finally:
        job.close()


def result_record(
    case: probes.CodexCase,
    evidence: probes.CodexEvidence,
    *,
    version: str | None,
    started_at: str,
    cleanup: Mapping[str, bool],
    interactive: bool = False,
) -> dict[str, Any]:
    verdict = probes.evaluate_codex(case, evidence)
    complete = all(cleanup.values())
    record: dict[str, Any] = probes.codex_record(
        case,
        evidence,
        verdict=verdict,
        version=version,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        removed=complete,
    )
    record["record_id"] = f"wd163-codex-cli-{case.name}-{uuid4().hex}"
    record["provenance"]["source"]["sanitized_reference"] = f"wd163-{case.name}"
    record["provenance"]["model"] = {"name": "gpt-6.1-sol", "reasoning_effort": "low"}
    record["result"]["limitations"] = [
        note
        for note in record["result"]["limitations"]
        if note != "One Windows Codex CLI version and one scripted prompt."
        and not (
            interactive and note == "The non-interactive runner may not expose manual compaction."
        )
    ] + ["One Windows CLI build and one isolated attempt; no cross-version inference."]
    record["scope"]["isolation"] = (
        "Isolated Codex home and scratch Git repository outside registered projects"
    )
    record["scope"]["notes"] = "Interactive CLI" if interactive else "Noninteractive codex exec"
    record["scope"]["expected_deliveries"] = 0
    callback = record["stages"]["callbacks"]
    callback["observed"] = sum(len(evidence.calls(event)) for event in case.expected)
    callback["expected"] = sum(
        max(minimum, len(evidence.calls(event))) for event, minimum in case.expected.items()
    )
    callback["missing"] = callback["expected"] - callback["observed"]
    record["scope"]["callback_minimums"] = dict(case.expected)
    record["stages"]["provider_dispatch"]["state"] = (
        "confirmed" if callback["observed"] else "missing" if evidence.completed else "failed"
    )
    record["stages"]["provider_dispatch"]["evidence"] = (
        "Observed hook callbacks and owned CLI exit status"
    )
    record["capabilities"][case.name]["observed_effect"] = verdict[0]
    record["scope"]["tool_path"] = "code_mode" if case.code_mode else "default"
    for event, _matcher in case.events:
        inputs = [item.get("tool_input") for item in evidence.calls(event)]
        record["capabilities"][case.name]["callbacks"][event]["tool_input_types"] = {
            key: type(value).__name__
            for item in inputs
            if isinstance(item, dict)
            for key, value in item.items()
        }
    if not complete:
        record["capabilities"][case.name]["state"] = "inconclusive"
        record["result"]["state"] = "incomplete"
    # This probe observes provider effects, not a Watchdog store delivery.
    record["stages"]["end_to_end_delivery"].update(
        expected=0,
        persisted=0,
        read_back=0,
        state="not_applicable",
        failure_attribution="not_applicable",
        failure_detail=None,
        evidence="No Watchdog store; provider effect is recorded separately in capabilities.",
    )
    record["effect"] = {
        "state": verdict[0],
        "evidence": "Filtered model/tool output and isolated sentinel read-back",
        "notes": verdict[1],
    }
    record["cleanup"] = {
        "resources": [
            {
                "kind": kind,
                "alias": alias,
                "action": ("stopped" if key == "process" else "removed")
                if cleanup[key]
                else "retained",
                "state": "confirmed" if cleanup[key] else "unknown",
                "failure_attribution": "not_applicable" if cleanup[key] else "unknown",
                "detail": None,
                "evidence": detail,
            }
            for key, kind, alias, detail in [
                (
                    "process",
                    "process",
                    "owned-cli-process-tree",
                    "Windows job accounting reports zero active processes",
                ),
                (
                    "profile",
                    "hook_installation",
                    "isolated-codex-home",
                    "Isolated home removal check; active profile was not modified",
                ),
                (
                    "scratch",
                    "temporary_state",
                    "scratch-and-rollouts",
                    "Scratch removal check, including authentication copy and provider rollouts",
                ),
            ]
        ],
        "complete": complete,
        "remaining_state": None if complete else "See cleanup resources",
    }
    return record


def rollout_facts(rollout: str) -> dict[str, object]:
    """Keep only fixed tool names and permission enums from same-run diagnostics."""
    tools: set[str] = set()
    permissions = None
    for line in rollout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if (
            item.get("type") == "response_item"
            and payload.get("type")
            in {
                "function_call",
                "custom_tool_call",
            }
            and name
            in {
                "exec",
                "exec_command",
                "apply_patch",
                "spawn_agent",
                "update_plan",
                "mcp__probe__emit",
            }
        ):
            tools.add(name)
        if item.get("type") == "turn_context":
            policy = payload.get("sandbox_policy", {})
            if isinstance(policy, dict) and policy.get("type") in {"workspace-write", "read-only"}:
                permissions = policy["type"]
    return {"observed_tool_paths": sorted(tools), "effective_sandbox": permissions}


def adapter_rendered_deny(record: Mapping[str, object]) -> bool:
    output = record.get("output")
    if not isinstance(output, dict):
        return False
    hook = output.get("hookSpecificOutput")
    return (
        isinstance(hook, dict)
        and hook.get("permissionDecision") == "deny"
        and record.get("adapter_exit_code") == 0
    )


def run_case(args, case):
    if os.name != "nt":
        raise RuntimeError("WD-163 owned-process runner currently requires Windows")
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    codex = probes.resolve_codex(args.codex)
    version = probes.codex_version(codex)
    production_adapter = getattr(args, "production_adapter", None)
    if production_adapter is not None:
        production.require_provider_version(version)
        if case.name != production.CASE or args.interactive:
            raise ValueError("production adapter currently supports only noninteractive patch deny")
        adapter_banner = production.adapter_version(production_adapter)
    else:
        adapter_banner = None
    roots = probes.registered_roots(args.config)
    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    scratch = Path(tempfile.mkdtemp(prefix="watchdog-wd163-"))
    home = scratch / "home"
    work = scratch / "work"
    log = scratch / "probe.ndjson"
    cleanup = {"process": False, "profile": False, "scratch": False}
    process = None
    dispatched = False
    job = None
    stdout = ""
    completed = False
    timed_out = False
    debug = {}
    facts = {}
    failure = None
    stack = ExitStack()
    readback: dict[str, int] = {}
    channel_schema = None
    evidence = probes.CodexEvidence(
        records=[], stdout="", rollout="", files=frozenset(), completed=False
    )
    try:
        probes.ensure_scratch_outside_roots(scratch, roots)
        home.mkdir()
        work.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(work)],
            check=True,
            creationflags=probes.hidden_creationflags(),
        )
        # Only the credential file is copied. Global hooks, plugins and MCP servers stay out.
        shutil.copyfile(source_home / "auth.json", home / "auth.json")
        config = 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "low"\n'
        if "mcp" in case.name:
            config += '[mcp_servers.probe]\ndefault_tools_approval_mode = "approve"\n'
            config += f"command = {json.dumps(sys.executable)}\n"
            config += (
                "args = "
                + json.dumps(
                    [
                        str(Path(__file__).with_name("codex_probe_mcp.py").resolve()),
                        "--work",
                        str(work),
                    ]
                )
                + "\n"
            )
        config += f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n'
        # The diagnostic callback supplies a same-run transcript path even when
        # the event under test never fires. It returns no control output.
        events = case.events
        if not any(event == "SessionStart" for event, _matcher in events):
            events = (*events, ("SessionStart", "^startup$"))
        configuration_case = replace(case, events=events)
        if production_adapter is None:
            config += probes.codex_profile_configuration(
                configuration_case, Path(probes.__file__).resolve(), log
            )
        else:
            paths, hooks = production.configure(scratch, work, log, production_adapter)
            config += hooks
            stack.enter_context(writer_lock(paths.data / "daemon.lock"))
            stack.enter_context(_policy_server(paths, _SessionTracker()))
            discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
            channel_schema = discovery.get("schema_version")
            if channel_schema != 2:
                raise RuntimeError("scratch decision channel did not publish schema 2")
        (home / "config.toml").write_text(config, encoding="utf-8", newline="\n")
        command = command_line(codex, case, work, interactive=args.interactive)
        print(
            f"WD-163 case {case.name}; isolated home; timeout {args.timeout}s",
            file=sys.stderr,
            flush=True,
        )
        environment = isolated_environment(home, os.environ)
        if args.interactive:
            environment["TERM"] = "xterm-256color"
        launcher = scratch / "launch.py"
        command_file = scratch / "command.json"
        gate = scratch / "launch-ready"
        launcher.write_text(LAUNCHER, encoding="utf-8", newline="\n")
        command_file.write_text(json.dumps(command), encoding="utf-8", newline="\n")
        process = subprocess.Popen(
            [
                sys.executable,
                str(launcher),
                str(gate),
                str(command_file),
                str(0 if args.interactive else probes.hidden_creationflags()),
            ],
            cwd=work,
            env=environment,
            stdin=None if args.interactive else subprocess.DEVNULL,
            stdout=None if args.interactive else subprocess.PIPE,
            stderr=None if args.interactive else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0 if args.interactive else probes.hidden_creationflags(),
        )
        job = OwnedProcessJob(process)
        gate.write_text("ready", encoding="utf-8")
        dispatched = True
        try:
            if args.interactive:
                process.wait(timeout=args.timeout)
            else:
                stdout, _stderr = process.communicate(timeout=args.timeout)
                debug["stderr"] = _stderr
            completed = process.returncode == 0
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            cleanup["process"] = stop_job(job, process)
            job = None
        records = probes._read_records(log)
        rollout = probes._read_rollout(probes._rollout_path(records))
        debug.update(stdout=stdout, rollout=rollout, callbacks=records)
        facts = rollout_facts(rollout)
        evidence = probes.CodexEvidence(
            records=records,
            stdout=stdout,
            rollout=observed_text(rollout),
            files=frozenset(item.name for item in work.iterdir() if item.name != ".git"),
            file_markers=probes._codex_file_markers(work),
            completed=completed,
            timed_out=timed_out,
        )
        if production_adapter is not None:
            stack.close()
            readback = production.readback(paths)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        failure = type(error).__name__
        debug["failure"] = str(error)
    finally:
        stack.close()
        if job is not None and process is not None:
            cleanup["process"] = stop_job(job, process)
        elif process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
            cleanup["process"] = not dispatched
        elif process is None:
            cleanup["process"] = True
        cleanup["scratch"] = probes._remove_codex_scratch(scratch)
        cleanup["profile"] = not home.exists()
    record = result_record(
        case,
        evidence,
        version=version,
        started_at=started_at,
        cleanup=cleanup,
        interactive=args.interactive,
    )
    if production_adapter is not None:
        record["record_id"] = f"wd151-production-{case.name}-{uuid4().hex}"
        record["provenance"]["source"]["sanitized_reference"] = "wd151-production-scratch"
        record["provenance"]["adapter"] = {
            "name": "agent-watchdog-hook",
            "version": adapter_banner,
            "invocation": "scratch Codex hook wrapper invoking native adapter",
        }
        record["scope"]["isolation"] = (
            "Scratch Codex home, project, Watchdog config, data, and runtime"
        )
        record["scope"]["notes"] = (
            "Real native adapter and in-process daemon decision channel; "
            "native spool ingested with daemon implementation"
        )
        record["scope"]["expected_deliveries"] = 1
        rendered = any(adapter_rendered_deny(item) for item in evidence.records)
        persisted = readback.get("persisted_controls", 0)
        delivery = record["stages"]["end_to_end_delivery"]
        record["stages"]["adapter"] = {
            "state": "confirmed" if rendered else "failed",
            "starts": sum(item.get("adapter_exit_code") is not None for item in evidence.records),
            "failure_category": None if rendered else "response_not_rendered",
            "evidence": "Native stdout parsed from same-run callback diagnostics",
        }
        record["stages"]["persistence"] = {
            "accepted": readback.get("spooled", 0),
            "losses": [],
            "evidence": "Native spool ingested directly via daemon _drain_spool and Store",
        }
        delivery.update(
            expected=1,
            persisted=persisted,
            read_back=persisted,
            state="confirmed" if persisted else "missing",
            failure_attribution="not_applicable" if persisted else "daemon_or_store",
            failure_detail=None,
            evidence=(
                "Approved deny envelope linked to native PreToolUse read back from scratch Store"
            ),
        )
        record["production_adapter"] = {
            "version": adapter_banner,
            "decision_schema": channel_schema,
            "control_profile": production.PROFILE,
            "callback_count": len(evidence.calls("PreToolUse")),
            "rendered_deny": rendered,
            "spooled_events": readback.get("spooled", 0),
            "spooled_patch_events": readback.get("spooled_patch_events", 0),
            "stored_control_events": readback.get("stored_controls", 0),
            "persisted_controls": persisted,
            "ingestion": "in-process daemon _drain_spool directly to Store",
        }
        if not (
            record["capabilities"][case.name]["observed_effect"] == "supported"
            and rendered
            and persisted > 0
            and readback.get("spooled", 0) > 0
            and readback.get("spooled_patch_events", 0) > 0
            and channel_schema == 2
            and record["cleanup"]["complete"]
        ):
            record["capabilities"][case.name]["state"] = "inconclusive"
            record["result"]["state"] = "incomplete"
            record["result"]["limitations"].append(
                "Production control requires adapter rendering, provider effect, "
                "persisted control, and cleanup"
            )
    record["execution"] = {
        "exit_code": process.returncode if process is not None else None,
        "timed_out": timed_out,
        **facts,
    }
    tool_paths = facts.get("observed_tool_paths")
    if case.code_mode and (not isinstance(tool_paths, list) or "exec" not in tool_paths):
        record["capabilities"][case.name]["state"] = "inconclusive"
        record["result"]["state"] = "incomplete"
        record["result"]["limitations"].append("No code-mode exec wrapper was read back")
    if failure:
        record["stages"]["provider_dispatch"]["detail"] = failure
        record["result"]["limitations"].append(f"Runner failure: {failure}")
    if not dispatched:
        record["stages"]["provider_dispatch"]["attempts"] = 0
        record["cleanup"]["resources"][0]["evidence"] = "No provider process was started"
    if args.debug_log:
        with args.debug_log.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(debug, indent=2) + "\n")
        record["cleanup"]["resources"].append(
            {
                "kind": "temporary_state",
                "alias": "operator-debug-log",
                "action": "retained",
                "state": "confirmed",
                "failure_attribution": "not_applicable",
                "detail": "Raw diagnostics retained in git-ignored tests/local only.",
                "evidence": "Exclusive debug-log write completed",
            }
        )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", required=True, help="Explicitly spend provider quota"
    )
    parser.add_argument("--case", required=True, choices=sorted(probes.CODEX_CASES))
    parser.add_argument("--codex", required=True, help="Explicit CLI executable to pin its version")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--production-adapter",
        type=Path,
        help="Opt in to scratch production-adapter patch-deny probe with an absolute native binary",
    )
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--debug-log", type=Path, help="Optional raw diagnostics under git-ignored tests/local"
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output already exists; choose a new attempt file")
    if not args.output.parent.is_dir():
        parser.error("--output parent directory does not exist")
    if args.debug_log:
        local = Path(__file__).resolve().parents[1] / "tests" / "local"
        if (
            not args.debug_log.resolve().is_relative_to(local)
            or args.debug_log.exists()
            or not args.debug_log.parent.is_dir()
        ):
            parser.error("--debug-log must be a new file under tests/local")
    case = probes.CODEX_CASES[args.case]
    if args.production_adapter is not None and (
        not args.production_adapter.is_absolute()
        or not args.production_adapter.is_file()
        or case.name != production.CASE
        or args.interactive
    ):
        parser.error(
            "--production-adapter requires an absolute binary and noninteractive patch-deny case"
        )
    command_line(args.codex, case, Path("."), interactive=args.interactive)
    record = run_case(args, case)
    # Exclusive creation prevents silently replacing an earlier attempt.
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "case": case.name,
                "state": record["capabilities"][case.name]["state"],
                "cleanup": record["cleanup"]["complete"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
