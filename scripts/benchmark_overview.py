"""Opt-in synthetic `overview` benchmark (WD-141); no provider invocation.

Builds a store of synthetic tool-loop sessions in a temporary directory, then times
the write path (with session-state upserts), the per-session findings refresh, the
`overview` command's read, and the per-provider `report` analysis that `overview`
used before schema v8.
"""

import argparse
import json
import platform
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agent_watchdog import inspection
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.events import Envelope, EventKind
from agent_watchdog.storage import Store

START = datetime(2026, 10, 1, tzinfo=UTC)


def session_events(project, provider: str, session: int, calls: int) -> list[Envelope]:
    """One session: a turn of ``calls`` tool calls, every third one repeating the previous."""
    session_id = f"{provider}-{session}"
    clock = START + timedelta(minutes=session)

    def event(kind: EventKind, offset: int, payload: dict | None = None) -> Envelope:
        return Envelope(
            provider=provider,
            project_id=project.id,
            session_id=session_id,
            kind=kind,
            source="hook",
            received_at=clock + timedelta(seconds=offset),
            payload={provider: payload or {}},
        )

    events = [event("turn.start", 0)]
    for call in range(calls):
        command = f"pytest tests/test_{call - call % 3}.py"
        response = {"exit_code": 1 if call % 3 else 0, "stdout": "FAILED tests/test_x.py::t"}
        events.append(event("tool.start", 1 + 2 * call, {"tool_name": "Bash"}))
        events.append(
            event(
                "tool.finish",
                2 + 2 * call,
                {
                    "tool_name": "Bash",
                    "content": {"tool_input": {"command": command}, "tool_response": response},
                },
            )
        )
    events.append(event("turn.end", 2 * calls + 3))
    return events


def timed(function, repeat: int = 3) -> float:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=15000)
    parser.add_argument("--calls-per-session", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    per_session = 2 * args.calls_per_session + 2
    sessions = max(2, args.events // per_session)

    with tempfile.TemporaryDirectory(prefix="watchdog-overview-bench-") as scratch:
        base = Path(scratch)
        project = Project(id=uuid4(), root=base / "project")
        save_config(base / "config.toml", Config(projects=(project,)))
        paths = UserPaths(base / "config.toml", base / "data", base / "runtime")
        events = [
            event
            for session in range(sessions)
            for event in session_events(
                project, ("codex", "claude")[session % 2], session, args.calls_per_session
            )
        ]
        with Store(paths.project_data(project.id), project.id) as store:
            started = time.perf_counter()
            for event in events:
                store.put(event)
            put_seconds = time.perf_counter() - started
            started = time.perf_counter()
            for provider, session_id in store.pending_sessions():
                store.refresh_session_findings(provider, session_id)
            refresh_seconds = time.perf_counter() - started
        overview = inspection.overview(paths, since=None, until=None)
        findings = overview["projects"][0]["findings"]
        result = {
            "host": platform.platform(),
            "python": platform.python_version(),
            "events": len(events),
            "sessions": sessions,
            "put_seconds": round(put_seconds, 3),
            "put_events_per_second": round(len(events) / put_seconds),
            "refresh_all_seconds": round(refresh_seconds, 3),
            "overview_seconds": round(
                timed(lambda: inspection.overview(paths, since=None, until=None)), 3
            ),
            "report_findings_seconds": round(
                timed(
                    lambda: [
                        inspection.report(paths, project, session_id=None, provider=provider)
                        for provider in ("codex", "claude")
                    ]
                ),
                3,
            ),
            "pending_sessions": findings["pending_sessions"],
            "by_rule": {name: item["by_rule"] for name, item in findings["by_provider"].items()},
        }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
