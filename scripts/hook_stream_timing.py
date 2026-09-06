"""Verify Claude hook pairing, outcomes, and tool-use ids from the stream.

Claude Code 2.1.259 does not write ``hookInfos``/``durationMs`` into ``-p`` JSON
transcripts (see docs/verification.md). It does emit, on
``--output-format stream-json --include-hook-events``, a ``hook_started`` record
and a matching ``hook_response`` record per hook invocation, keyed by
``hook_id``. This tool pairs those records and reports counts, per-event tallies,
and outcomes.

It does **not** report a timing delta. The read-time gap between the two stream
records is dominated by Claude's stdout-flush cadence, not by hook wall time
(a sequential run measured slower than a four-way concurrent one), so it is not a
substitute for a harness-reported duration. A monotonic read stamp is still
written into the capture NDJSON for provenance, but ``report`` ignores it.

Subcommands:
  capture  run one headless Claude session, writing an NDJSON of its hook + tool
           records (ids, counts, outcomes only; never prompt, command or tool
           output text).
  report   read one or more capture NDJSON files and print the verification JSON.

Never writes into ``~/.claude``.
"""

import argparse
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = 2
HOOK_SUBTYPES = ("hook_started", "hook_response")
_STAMP = "readMonotonicNs"


def _keep_hook(record: dict, stamp: int) -> dict:
    return {
        _STAMP: stamp,
        "type": "hook",
        "subtype": record.get("subtype"),
        "hook_id": record.get("hook_id"),
        "hook_name": record.get("hook_name"),
        "hook_event": record.get("hook_event"),
        "exit_code": record.get("exit_code"),
        "outcome": record.get("outcome"),
        "stdout_len": len(record["stdout"]) if isinstance(record.get("stdout"), str) else None,
        "session_id": record.get("session_id"),
    }


def _tool_use_ids(node: object, sink: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "tool_use" and isinstance(node.get("id"), str):
            sink.add(node["id"])
        value = node.get("tool_use_id")
        if isinstance(value, str) and value:
            sink.add(value)
        for child in node.values():
            _tool_use_ids(child, sink)
    elif isinstance(node, list):
        for child in node:
            _tool_use_ids(child, sink)


def capture(args: argparse.Namespace) -> int:
    command = [
        args.claude_bin,
        "-p",
        args.prompt,
        "--settings",
        args.settings,
        "--setting-sources",
        args.setting_sources,
        "--allowedTools",
        "Bash",
        "--permission-prompts",
        "none",
        "--session-id",
        args.session_id,
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--verbose",
    ]
    for directory in args.add_dir:
        command += ["--add-dir", directory]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tool_ids: set[str] = set()
    final: dict = {}
    with (
        args.out.open("w", encoding="utf-8", newline="\n") as sink,
        subprocess.Popen(
            command,
            cwd=args.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        ) as proc,
    ):
        assert proc.stdout is not None
        for line in proc.stdout:
            stamp = time.monotonic_ns()
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            _tool_use_ids(record, tool_ids)
            if record.get("type") == "system" and record.get("subtype") in HOOK_SUBTYPES:
                sink.write(json.dumps(_keep_hook(record, stamp)) + "\n")
            elif record.get("type") == "result":
                final = {
                    "type": "run",
                    "session_id": record.get("session_id"),
                    "num_turns": record.get("num_turns"),
                    "is_error": record.get("is_error"),
                    "duration_ms": record.get("duration_ms"),
                    "permission_denials": len(record.get("permission_denials") or []),
                }
        code = proc.wait()
    with args.out.open("a", encoding="utf-8", newline="\n") as sink:
        for tool_id in sorted(tool_ids):
            sink.write(json.dumps({"type": "tool_use_id", "id": tool_id}) + "\n")
        sink.write(json.dumps(final or {"type": "run", "exit_code": code}) + "\n")
    return code


def _load(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def report(args: argparse.Namespace) -> int:
    rows = _load(args.stream)
    started: dict[str, dict] = {}
    paired = 0
    per_event: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    nonempty_stdout = 0
    unpaired_started = 0
    runs = 0
    errored_runs = 0
    tool_ids: set[str] = set()
    for row in rows:
        kind = row.get("type")
        if kind == "tool_use_id" and isinstance(row.get("id"), str):
            tool_ids.add(row["id"])
        elif kind == "run":
            runs += 1
            errored_runs += 1 if row.get("is_error") else 0
        elif kind == "hook":
            hook_id = row.get("hook_id")
            if not isinstance(hook_id, str):
                continue
            if row.get("subtype") == "hook_started":
                started[hook_id] = row
            elif row.get("subtype") == "hook_response":
                begin = started.pop(hook_id, None)
                per_event[row.get("hook_event") or "unknown"] += 1
                outcomes[row.get("outcome") or "unknown"] += 1
                if row.get("stdout_len"):
                    nonempty_stdout += 1
                if begin is not None:
                    paired += 1
    unpaired_started = len(started)
    result = {
        "schema_version": SCHEMA_VERSION,
        "streams": len(args.stream),
        "runs": runs,
        "errored_runs": errored_runs,
        "hook_pairs": paired,
        "unpaired_hook_started": unpaired_started,
        "per_event": dict(sorted(per_event.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "nonempty_stdout_responses": nonempty_stdout,
        "tool_use_id_count": len(tool_ids),
        "tool_use_ids": sorted(tool_ids),
    }
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    cap = sub.add_parser("capture")
    cap.add_argument("--claude-bin", required=True)
    cap.add_argument("--session-id", required=True)
    cap.add_argument("--cwd", required=True)
    cap.add_argument("--settings", required=True)
    cap.add_argument("--setting-sources", default="")
    cap.add_argument("--add-dir", action="append", default=[])
    cap.add_argument("--prompt", required=True)
    cap.add_argument("--out", type=Path, required=True)
    cap.set_defaults(func=capture)

    rep = sub.add_parser("report")
    rep.add_argument("stream", type=Path, nargs="+")
    rep.add_argument("--output", type=Path)
    rep.set_defaults(func=report)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
