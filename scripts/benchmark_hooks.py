"""Opt-in synthetic hook benchmark; no provider invocation or hook installation."""

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=15, check=True, **kwargs)


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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 20 or not 1 <= args.idle_seconds <= 60:
        parser.error("Require at least 20 samples and 1..60 idle seconds")
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
        project = json.loads(run(command + ["project", "add", str(root)]).stdout)
        try:
            run(command + ["daemon", "start"])

            def hook(index: int) -> float:
                payload = {
                    "cwd": str(root if index % 2 else worktree),
                    "session_id": f"benchmark-{index % 2}",
                    "hook_event_name": "Stop",
                }
                start = time.perf_counter()
                result = run(command + ["hook", "codex"], input=json.dumps(payload))
                elapsed = (time.perf_counter() - start) * 1000
                assert result.stdout.strip() == "{}"
                return elapsed

            first = hook(0)
            sequential = [hook(index) for index in range(args.samples)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                concurrent = list(pool.map(hook, range(args.samples)))
            expected = 1 + 2 * args.samples
            deadline = time.monotonic() + 15
            while True:
                sessions = json.loads(
                    run(command + ["sessions", "list", "--project", project["id"]]).stdout
                )
                if sum(item["event_count"] for item in sessions["sessions"]) == expected:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic deliveries were lost or did not drain")
                time.sleep(0.1)
            state = json.loads(run(command + ["daemon", "status"]).stdout)
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
            run(command + ["daemon", "start"])
            reopened = json.loads(
                run(command + ["sessions", "list", "--project", project["id"]]).stdout
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
                "schema_version": 1,
                "python": platform.python_version(),
                "os": platform.platform(),
                "scope": (
                    "synthetic hook process wall time including Python and Git; "
                    "daemon already running"
                ),
                "first_ms": first,
                "sequential": distribution(sequential),
                "concurrent_four": distribution(concurrent),
                "idle": idle,
                "events": expected,
                "sessions": 2,
                "checkouts": 2,
                "restart_preserved": True,
                "losses": state["losses"],
                "native_provider_validation": False,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report))
        finally:
            run(command + ["daemon", "stop"])


if __name__ == "__main__":
    main()
