"""Opt-in Windows shell startup floor: time a no-op shell launch without the adapter.

Separates the provider-selected shell's own startup cost from Watchdog's. It
invokes no provider and installs no hook.
"""

import argparse
import json
import math
import platform
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_watchdog._proc import hidden_creationflags

# The first entry mirrors the flags Codex passes to PowerShell hooks as far as
# the public issue tracker documents them (`-NoProfile -Command`); the others
# are comparison points, not Codex configuration options.
VARIANTS = {
    "powershell.exe -NoProfile -Command": ["powershell.exe", "-NoProfile", "-Command", "exit"],
    "powershell.exe -NoLogo -NoProfile -NonInteractive -Command": [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "exit",
    ],
    "powershell.exe -Command (profile loaded)": ["powershell.exe", "-Command", "exit"],
    "cmd.exe /d /s /c": ["cmd.exe", "/d", "/s", "/c", "exit"],
}


def launch_ms(command: list[str]) -> float:
    started = time.perf_counter()
    subprocess.run(
        command, capture_output=True, check=True, timeout=15, creationflags=hidden_creationflags(0)
    )
    return (time.perf_counter() - started) * 1000


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(ordered[len(ordered) // 2], 1),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 1),
        "max_ms": round(ordered[-1], 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = {}
    for name, command in VARIANTS.items():
        first = launch_ms(command)
        sequential = [launch_ms(command) for _ in range(args.samples)]
        with ThreadPoolExecutor(4) as pool:
            four_way = list(pool.map(launch_ms, [command] * args.samples))
        results[name] = {
            "first_ms": round(first, 1),
            "sequential": summarize(sequential),
            "concurrent_four": summarize(four_way),
        }
    report = {
        "schema_version": 1,
        "os": platform.platform(),
        "scope": "no-op shell launch only; no adapter, no provider, no hook installation",
        "launches": results,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
