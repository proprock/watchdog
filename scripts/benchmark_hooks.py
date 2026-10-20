"""Opt-in synthetic hook benchmark; no provider invocation or hook installation."""

import argparse
import hashlib
import json
import math
import os
import platform
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from agent_watchdog._proc import hidden_creationflags
from agent_watchdog.config import UserPaths

SHELLS = ("direct", "bash", "cmd.exe", "powershell.exe", "pwsh.exe")


def run(command: list[str] | str, **kwargs) -> subprocess.CompletedProcess:
    if os.name == "nt":
        kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags", 0))
    return subprocess.run(command, capture_output=True, text=True, timeout=15, check=True, **kwargs)


def hook_invocation(shell: str, arguments: list[str]) -> list[str] | str:
    """Wrap the adapter argument list for the requested launch mode.

    `direct` is exactly what Claude's exec form does; `bash -c` mirrors Claude's
    default Windows-with-Git-Bash path. The Windows shell forms let us compare
    CMD and PowerShell command-string startup without changing a provider hook.
    """
    if shell == "direct":
        return list(arguments)
    if shell == "bash":
        return ["bash", "-c", shlex.join(arguments)]
    if shell == "cmd.exe":
        # Keep argv[0] quoted even without spaces. ``cmd /c`` otherwise folds the
        # first command token and its following arguments into a program name.
        # A string prevents Python from escaping the inner quotes while it starts
        # cmd.exe; the outer quotes are part of CMD's documented /s /c grammar.
        command = " ".join(f'"{argument.replace(chr(34), chr(34) * 2)}"' for argument in arguments)
        return f'{shell} /d /s /c "{command}"'
    quoted = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
    return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", quoted]


EVENTS = ("Stop", "PostToolUse", "PreToolUse")


def hook_payload(event: str, cwd: str, session_id: str, index: int, *, tool: str = "Bash") -> dict:
    """One synthetic hook input.

    `Stop` is unsubscribed in the scratch default configuration. Tool events use
    Bash by default; Agent and apply_patch cover the other measured cells. A
    scratch rule can provide a subscription without matching these inputs.
    Inputs differ per call so the repeat rule cannot fire.
    """
    payload: dict = {"cwd": cwd, "session_id": session_id, "hook_event_name": event}
    if event != "Stop":
        tool_input = {"command": f"echo benchmark {index}"}
        if tool == "Agent":
            tool_input = {"prompt": f"Synthetic benchmark {index}", "model": "haiku"}
        elif tool == "apply_patch":
            tool_input = {
                "command": f"*** Begin Patch\n*** Add File: bench-{index}\n+x\n*** End Patch"
            }
        payload |= {"tool_name": tool, "tool_input": tool_input}
    if event == "PostToolUse":
        payload["tool_response"] = {"stdout": f"benchmark {index}", "exit_code": 0}
    return payload


def prepare_subscription(paths: UserPaths, provider: str, event: str, tool: str) -> None:
    """Approve one synthetic, never-matching rule in the benchmark's scratch home."""
    import tomli_w

    from agent_watchdog.rules.manage import approve

    rule = {
        "schema_version": 1,
        "name": "benchmark_subscription",
        "version": "1",
        "origin": "user",
        "provider": [provider],
        "event": event,
        "match": {"tool_name": tool, "input_regex": "WD154_NEVER_MATCH"},
        "action": {"kind": "context", "message": "Synthetic benchmark only."},
    }
    path = paths.config.parent / "rules" / "benchmark_subscription.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(rule), encoding="utf-8", newline="\n")
    approve(paths, rule["name"], hashlib.sha256(path.read_bytes()).hexdigest())


def decision_round_trip(
    discovery: dict, provider: str, payload: dict, profile: str | None
) -> float:
    """Time a Python socket client, excluding process launch and discovery-file reading.

    This complements native whole-hook timing; it does not instrument the native client.
    A missing/malformed answer fails the benchmark rather than looking like a fast allow.
    """
    request = {
        "schema_version": 2,
        "token": discovery["token"],
        "provider": provider,
        "codex_control_profile": profile,
        "event_id": str(uuid4()),
        "input": payload,
    }
    wire = json.dumps(request).encode() + b"\n"
    start = time.perf_counter()
    deadline = time.monotonic() + discovery["timeout_ms"] / 1000
    with socket.create_connection(
        ("127.0.0.1", discovery["port"]), timeout=deadline - time.monotonic()
    ) as stream:
        stream.settimeout(max(0.000001, deadline - time.monotonic()))
        stream.sendall(wire)
        stream.shutdown(socket.SHUT_WR)
        response = bytearray()
        while b"\n" not in response:
            stream.settimeout(max(0.000001, deadline - time.monotonic()))
            part = stream.recv(16385 - len(response))
            if not part:
                break
            if len(response) + len(part) > 16384:
                raise RuntimeError("Missing or oversized decision response")
            response.extend(part)
    elapsed = (time.perf_counter() - start) * 1000
    if not response:
        raise RuntimeError("Missing or oversized decision response")
    if json.loads(response).get("action") != "allow":
        raise RuntimeError("Synthetic no-match request did not receive allow")
    return elapsed


def warm_up(hook, count: int) -> int:
    """Run untimed hook calls, sequential then four-way, so caches and the daemon settle."""
    for index in range(count):
        hook(index)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(hook, range(count)))
    return 2 * count


def cpu_busy_percent(before: tuple[int, int, int], after: tuple[int, int, int]) -> float:
    """Busy share between two (idle, kernel, user) counters; kernel time includes idle."""
    idle = after[0] - before[0]
    total = (after[1] - before[1]) + (after[2] - before[2])
    if total <= 0:
        raise ValueError("no elapsed CPU time between the two readings")
    return 100.0 * (total - idle) / total


def _system_times() -> tuple[int, int, int] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    return tuple((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (idle, kernel, user))  # ty: ignore[invalid-return-type]


def host_cpu_percent(seconds: float) -> float | None:
    """Whole-host CPU busy share over a short window; None where unsupported."""
    before = _system_times()
    if before is None:
        return None
    time.sleep(seconds)
    after = _system_times()
    return None if after is None else cpu_busy_percent(before, after)


def settle_host(
    limit: float | None,
    seconds: float,
    timeout: float,
    sample=host_cpu_percent,
    clock=time.monotonic,
) -> float | None:
    """Sample host CPU until it is at or below `limit`; fail once `timeout` seconds pass."""
    deadline = clock() + timeout
    while True:
        percent = sample(seconds)
        if percent is None or limit is None or percent <= limit:
            return percent
        if clock() >= deadline:
            raise RuntimeError(f"Host stayed busy: {percent:.0f}% CPU above the {limit:.0f}% limit")


def wait_for_daemon(command: list[str]) -> dict:
    # Start returns before readiness, and a startup contender can invalidate status.
    deadline = time.monotonic() + 15
    while True:
        report = json.loads(run(command + ["daemon", "status"]).stdout)
        if report.get("state") == "running" and report.get("alive") and report.get("pid"):
            return report
        if time.monotonic() >= deadline:
            raise RuntimeError("Benchmark daemon did not become ready")
        time.sleep(0.1)


def process_metrics(pid: int) -> dict | None:
    if os.name != "nt":
        return None
    result = run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Get-Process -Id {pid} | Select-Object CPU,WorkingSet64 | ConvertTo-Json -Compress",
        ]
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--idle-seconds", type=float, default=20)
    parser.add_argument(
        "--warmup", type=int, default=8, help="Untimed calls per phase before timing"
    )
    parser.add_argument("--cpu-seconds", type=float, default=3, help="Host CPU sampling window")
    parser.add_argument(
        "--max-host-cpu", type=float, help="Wait until host CPU is at or below this percent"
    )
    parser.add_argument(
        "--settle-timeout", type=float, default=60, help="Seconds to wait for --max-host-cpu"
    )
    parser.add_argument(
        "--event",
        choices=EVENTS,
        default="Stop",
        help="Hook event to send: Stop is unsubscribed (the zero path); PostToolUse and "
        "PreToolUse (Bash) are subscribed by the built-in rules",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter-executable", type=Path)
    parser.add_argument("--tool", choices=("Bash", "Agent", "apply_patch"), default="Bash")
    parser.add_argument(
        "--codex-control-profile", help="Exact evidenced CLI profile; synthetic only"
    )
    parser.add_argument(
        "--subscribe",
        action="store_true",
        help="Install a never-matching scratch rule, verify native requests, and time socket RTT",
    )
    parser.add_argument(
        "--benchmark-http",
        action="store_true",
        help="Opt-in direct native HTTP experiment in scratch state only",
    )
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument(
        "--shell",
        choices=SHELLS,
        default="direct",
        help="Wrap the generated hook command in this launch mode",
    )
    args = parser.parse_args()
    if args.samples < 20 or not 1 <= args.idle_seconds <= 60:
        parser.error("Require at least 20 samples and 1..60 idle seconds")
    if args.warmup < 0 or args.cpu_seconds <= 0:
        parser.error("Require a non-negative warmup and a positive CPU window")
    if args.subscribe and (not args.adapter_executable or args.event == "Stop"):
        parser.error("--subscribe requires a native adapter and a tool event")
    if args.codex_control_profile and (args.provider != "codex" or not args.adapter_executable):
        parser.error("--codex-control-profile requires the native Codex adapter")
    if args.benchmark_http and (not args.adapter_executable or args.event == "Stop"):
        parser.error("--benchmark-http requires --adapter-executable and a subscribed tool event")
    if args.shell in ("powershell.exe", "pwsh.exe") and os.name != "nt":
        parser.error("PowerShell launch measurements require Windows")
    with tempfile.TemporaryDirectory(prefix="wd008 benchmark ") as directory:
        home = Path(directory)
        root = home / "source"
        run(["git", "init", str(root)])
        run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Watchdog probe",
                "-c",
                "user.email=probe@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "probe",
            ]
        )
        worktree = home / "worktree"
        run(["git", "-C", str(root), "worktree", "add", "--detach", str(worktree)])
        command = [sys.executable, "-m", "agent_watchdog", "--home", str(home / "state")]
        state_home = home / "state"
        if args.adapter_executable:
            paths = UserPaths(
                state_home / "config.toml", state_home / "data", state_home / "runtime"
            )
            arguments = [
                str(args.adapter_executable.resolve()),
                "--python",
                sys.executable,
                "--config",
                str(paths.config),
                "--data",
                str(paths.data),
                "--runtime",
                str(paths.runtime),
                "hook",
                args.provider,
            ]
            if args.codex_control_profile:
                arguments += ["--codex-control-profile", args.codex_control_profile]
        else:
            arguments = command + ["hook", args.provider]
        hook_command = hook_invocation(args.shell, arguments)
        expected_stdout = "" if args.provider == "claude" else "{}"
        project = json.loads(run(command + ["project", "add", str(root)]).stdout)
        if args.subscribe:
            prepare_subscription(paths, args.provider, args.event, args.tool)
        hook_env = dict(os.environ)
        hook_env.pop("WATCHDOG_BENCHMARK_HTTP", None)
        if args.benchmark_http:
            hook_env["WATCHDOG_BENCHMARK_HTTP"] = "1"
        try:
            run(command + ["daemon", "start"], env=hook_env)
            wait_for_daemon(command)
            initial_cpu = settle_host(args.max_host_cpu, args.cpu_seconds, args.settle_timeout)

            def hook(index: int) -> float:
                payload = hook_payload(
                    args.event,
                    str(root if index % 2 else worktree),
                    f"benchmark-{index % 2}",
                    index,
                    tool=args.tool,
                )
                start = time.perf_counter()
                result = run(hook_command, input=json.dumps(payload), env=hook_env)
                elapsed = (time.perf_counter() - start) * 1000
                assert result.stdout.strip() == expected_stdout
                return elapsed

            first = hook(0)
            warmup_calls = warm_up(hook, args.warmup)
            cpu_percent = settle_host(args.max_host_cpu, args.cpu_seconds, args.settle_timeout)
            cpu_before = _system_times()
            sequential = [hook(index) for index in range(args.samples)]
            cpu_after = _system_times()
            seq_cpu = (
                None
                if cpu_before is None or cpu_after is None
                else cpu_busy_percent(cpu_before, cpu_after)
            )
            concurrent_ready_cpu = settle_host(
                args.max_host_cpu, args.cpu_seconds, args.settle_timeout
            )
            cpu_before = _system_times()
            with ThreadPoolExecutor(max_workers=4) as pool:
                concurrent = list(pool.map(hook, range(args.samples)))
            cpu_after = _system_times()
            concurrent_cpu = (
                None
                if cpu_before is None or cpu_after is None
                else cpu_busy_percent(cpu_before, cpu_after)
            )
            expected = 1 + warmup_calls + 2 * args.samples
            deadline = time.monotonic() + 15
            while True:
                sessions = json.loads(
                    run(command + ["sessions", "list", "--project", project["project"]]).stdout
                )
                if sum(item["event_count"] for item in sessions["sessions"]) == expected:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic deliveries were lost or did not drain")
                time.sleep(0.1)
            checkouts = set()
            for session in sessions["sessions"]:
                details = json.loads(
                    run(
                        command
                        + [
                            "sessions",
                            "show",
                            session["session_id"],
                            "--project",
                            project["project"],
                            "--provider",
                            args.provider,
                        ]
                    ).stdout
                )
                checkouts.update(event["checkout_id"] for event in details["events"])
            assert len(sessions["sessions"]) == 2 and len(checkouts) == 2
            state = wait_for_daemon(command)
            decision_requests = state.get("decision_channel", {}).get("requests", 0)
            if args.subscribe and decision_requests != expected:
                raise RuntimeError(
                    f"Only {decision_requests}/{expected} hooks reached the decision channel"
                )
            channel_errors = (
                max(0, expected - decision_requests)
                if args.adapter_executable and args.event != "Stop"
                else 0
            )
            round_trip = None
            if args.subscribe:
                discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
                rtt_ready_cpu = settle_host(
                    args.max_host_cpu, args.cpu_seconds, args.settle_timeout
                )

                def request(index: int) -> float:
                    payload = hook_payload(
                        args.event, str(root), f"rtt-{index}", index, tool=args.tool
                    )
                    return decision_round_trip(
                        discovery, args.provider, payload, args.codex_control_profile
                    )

                rtt_sequential = [request(index) for index in range(args.samples)]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    rtt_concurrent = list(pool.map(request, range(args.samples, 2 * args.samples)))
                round_trip = {
                    "scope": "Python socket client; excludes native launch, spool and discovery",
                    "budget_ms": discovery["timeout_ms"],
                    "ready_host_cpu_percent": rtt_ready_cpu,
                    "sequential": rtt_sequential,
                    "concurrent_four": rtt_concurrent,
                    "responses": 2 * args.samples,
                }
            before = process_metrics(state["pid"])
            start = time.monotonic()
            time.sleep(args.idle_seconds)
            after = process_metrics(state["pid"])
            idle_elapsed = time.monotonic() - start
            idle = {"seconds": idle_elapsed, "cpu_seconds": None, "working_set_bytes": None}
            if before is not None and after is not None:
                idle.update(
                    cpu_seconds=after["CPU"] - before["CPU"],
                    working_set_bytes=after["WorkingSet64"],
                )
            run(command + ["daemon", "stop"])
            run(command + ["daemon", "start"], env=hook_env)
            wait_for_daemon(command)
            reopened = json.loads(
                run(command + ["sessions", "list", "--project", project["project"]]).stdout
            )
            assert reopened == sessions

            def distribution(values: list[float]) -> dict:
                ordered = sorted(values)
                return {
                    "samples": len(values),
                    "p50_ms": ordered[math.ceil(len(values) * 0.5) - 1],
                    "p95_ms": ordered[math.ceil(len(values) * 0.95) - 1],
                    "max_ms": max(values),
                }

            report = {
                "schema_version": 2,
                "python": platform.python_version(),
                "os": platform.platform(),
                "scope": (
                    "synthetic hook process wall time including selected launch, runtime and Git; "
                    "daemon already running"
                ),
                "process_timeout_seconds": 15,
                "adapter": "rust" if args.adapter_executable else "python",
                "transport": "benchmark_http" if args.benchmark_http else "current",
                "provider": args.provider,
                "event": args.event,
                "tool": None if args.event == "Stop" else args.tool,
                "codex_control_profile": args.codex_control_profile,
                "surface": "synthetic_direct_adapter",
                "adapter_version": run(
                    [str(args.adapter_executable.resolve()), "--version"]
                ).stdout.strip()
                if args.adapter_executable
                else None,
                "launch": args.shell,
                "first_ms": first,
                "warmup_calls": warmup_calls,
                "host_cpu_percent": cpu_percent,
                "host_cpu": {
                    "before_first": initial_cpu,
                    "before_concurrent": concurrent_ready_cpu,
                    "during_sequential": seq_cpu,
                    "during_concurrent": concurrent_cpu,
                },
                "sequential": distribution(sequential),
                "concurrent_four": distribution(concurrent),
                "raw_ms": {"sequential": sequential, "concurrent_four": concurrent},
                "decision_round_trip": round_trip,
                "idle": idle,
                "events": expected,
                "sessions": 2,
                "checkouts": len(checkouts),
                "restart_preserved": True,
                "delivery_complete": True,
                "errors": channel_errors,
                "decision_requests": decision_requests,
                "losses": state["losses"],
                "native_provider_validation": False,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(json.dumps(report))
        finally:
            run(command + ["daemon", "stop"])


if __name__ == "__main__":
    main()
