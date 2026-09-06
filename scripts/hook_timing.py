"""Read-only Claude transcript analyzer for Watchdog hook timing.

Given a Claude transcript directory (``~/.claude/projects/<slug>/``) and an
installation token, report the harness-measured ``durationMs`` distribution for
Watchdog's hook, the tool-use ids present, and per-event invocation counts.

Hard rules:
- Never writes into the transcript directory or anywhere under ``~/.claude``.
- Emits ids, counts and durations only. Prompts, commands, paths and tool
  output are never copied into the report.
- Caps the bytes read per file and skips unparsable lines without failing.
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024**2
EVENT_KEYS = ("hookEventName", "hook_event_name", "eventName", "event")


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": max(ordered),
    }


def collect_tool_use_ids(node: object, sink: set[str]) -> None:
    if isinstance(node, dict):
        value = node.get("tool_use_id")
        if isinstance(value, str) and value:
            sink.add(value)
        if node.get("type") == "tool_use" and isinstance(node.get("id"), str):
            sink.add(node["id"])
        for child in node.values():
            collect_tool_use_ids(child, sink)
    elif isinstance(node, list):
        for child in node:
            collect_tool_use_ids(child, sink)


def event_name(record: dict, entry: dict) -> str:
    for source in (entry, record):
        for key in EVENT_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unidentified"


def iter_lines(directory: Path, max_bytes: int) -> tuple[list[str], int, int]:
    files = sorted(p for p in directory.rglob("*.jsonl") if p.is_file())
    lines: list[str] = []
    read = 0
    for path in files:
        budget = max_bytes
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                budget -= len(line.encode("utf-8", "replace"))
                if budget < 0:
                    break
                read += 1
                lines.append(line)
    return lines, len(files), read


def analyze(directory: Path, token: str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    raw_lines, file_count, read = iter_lines(directory, max_bytes)
    skipped = 0
    durations: list[float] = []
    missing_duration = 0
    matched_entries = 0
    per_event: Counter[str] = Counter()
    tool_use_ids: set[str] = set()
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        collect_tool_use_ids(record, tool_use_ids)
        infos = record.get("hookInfos")
        if not isinstance(infos, list):
            continue
        for entry in infos:
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if not isinstance(command, str) or token not in command:
                continue
            matched_entries += 1
            per_event[event_name(record, entry)] += 1
            duration = entry.get("durationMs")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                durations.append(float(duration))
            else:
                missing_duration += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "transcript_files": file_count,
        "lines_read": read,
        "lines_skipped": skipped,
        "matched_hook_entries": matched_entries,
        "duration_ms": distribution(durations),
        "missing_duration": missing_duration,
        "per_event": dict(sorted(per_event.items())),
        "tool_use_id_count": len(tool_use_ids),
        "tool_use_ids": sorted(tool_use_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript_dir", type=Path, help="Claude ~/.claude/projects/<slug>/ dir")
    parser.add_argument(
        "--token", required=True, help="Installation token to match in hook commands"
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--output", type=Path, help="Write the report here in addition to stdout")
    args = parser.parse_args()
    if not args.transcript_dir.is_dir():
        parser.error("transcript_dir must be an existing directory")
    if args.max_bytes < 1024:
        parser.error("--max-bytes must be at least 1024")
    report = analyze(args.transcript_dir, args.token, args.max_bytes)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")


if __name__ == "__main__":
    main()
