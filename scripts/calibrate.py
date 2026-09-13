"""Opt-in WD-012 calibration: freeze a session sample, review it, and report.

The exercise measures whether the deterministic shadow findings correspond to a
human judgement of the session.  It reads the collected store read-only, writes
annotations through the running core, and never invokes a provider.

Subcommands:
  sample    select a reproducible cohort and freeze its findings
  annotate  review the frozen cohort interactively
  report    join the cohort with recorded annotations and emit the evidence
"""

import argparse
import csv
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_watchdog import daemon, inspection
from agent_watchdog.analysis import (
    RULE_VERSION,
    RULES,
    analyze,
    captured_content,
    exit_code,
    tool_outcome,
)
from agent_watchdog.config import UserPaths, user_paths
from agent_watchdog.storage import FINDING_VERDICTS, PROGRESS_STATES, persisted_envelope

SAMPLE_FORMAT = "wd-012.sample.v1"
CALIBRATION_FORMAT = "wd-012.calibration.v2"
TASK_OUTCOMES = ("success", "partial", "failed", "abandoned", "unknown")
# A diff oscillation is evidence over a checkout's diff history, not a session's.
CHECKOUT_SCOPED_RULES = ("diff_oscillation",)

PROGRESS_HINTS = {
    "progress": "kept moving toward the task",
    "slow": "advanced, but wasted turns on repetition or rework",
    "stuck": "stopped advancing and repeated itself",
    "externally_blocked": "waited on something outside the agent's control",
}
VERDICT_HINTS = {
    "true_positive": "describes something that really went wrong",
    "false_positive": "fired on normal, healthy work",
    "uncertain": "the evidence does not settle it either way",
}
# How much of the session a card shows before the reviewer asks for more.
CARD_TAIL = 5
TIMELINE_TAIL = 20
EVIDENCE_SNIPPETS = 5
TRANSCRIPT_SUFFIXES = (".jsonl", ".json", ".log", ".md", ".txt")


def parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_sessions(db, since: datetime | None, until: datetime | None) -> dict:
    """Group stored events by (provider, session_id) within the requested window."""
    grouped: dict[tuple[str, str], list] = {}
    query = (
        "SELECT json_extract(envelope, '$.provider'), session_id, envelope "
        "FROM events WHERE session_id IS NOT NULL ORDER BY rowid"
    )
    for provider, session_id, document in db.execute(query):
        event = persisted_envelope(document)
        if since is not None and event.received_at < since:
            continue
        if until is not None and event.received_at > until:
            continue
        grouped.setdefault((provider, session_id), []).append(event)
    return grouped


def snapshots_for(db, checkouts: set[str]) -> list[dict]:
    if not checkouts:
        return []
    placeholders = ",".join("?" for _ in checkouts)
    return [
        {"snapshot_id": snapshot_id, "checkout_id": checkout_id, "fingerprint": fingerprint}
        for snapshot_id, checkout_id, fingerprint in db.execute(
            "SELECT snapshot_id, checkout_id, fingerprint FROM diff_snapshots "
            f"WHERE checkout_id IN ({placeholders}) ORDER BY observed_at, rowid",
            tuple(checkouts),
        )
    ]


def summarize(events: list) -> dict[str, Any]:
    kinds = Counter(event.kind for event in events)
    checkouts = sorted({str(event.checkout_id) for event in events if event.checkout_id})
    failures = sum(
        1
        for event in events
        if event.kind == "tool.finish" and "PostToolUseFailure" in json.dumps(event.payload)
    )
    return {
        "event_count": len(events),
        "turns": kinds["turn.start"],
        "tools": kinds["tool.start"],
        "tool_failures": failures,
        "waiting": kinds["waiting"],
        "gaps": kinds["observation.gap"],
        "compactions": kinds["compaction.start"],
        "first_received_at": events[0].received_at.isoformat(),
        "last_received_at": events[-1].received_at.isoformat(),
        "closed": bool(kinds["session.end"]),
        "checkout_ids": checkouts,
    }


def split_findings(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    session_scoped = [f for f in findings if f["rule"] not in CHECKOUT_SCOPED_RULES]
    checkout_scoped = [f for f in findings if f["rule"] in CHECKOUT_SCOPED_RULES]
    return session_scoped, checkout_scoped


def build_sample(paths: UserPaths, args: argparse.Namespace) -> dict:
    """Select a reproducible cohort and freeze the findings observed for it.

    Findings are recomputed on every read, so an open session can grow a group
    and change its fingerprint.  Only settled sessions enter the cohort, and the
    finding set is stored here so a review annotates what was actually seen.
    """
    project = inspection.project_at(paths, args.project)
    since, until = parse_time(args.since), parse_time(args.until)
    settled_before = datetime.now(UTC) - timedelta(hours=args.settle_hours)
    records: list[dict] = []
    checkout_findings: dict[str, dict] = {}
    skipped: Counter[str] = Counter()
    with inspection.database(paths, project) as db:
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        grouped = read_sessions(db, since, until)
        for (provider, session_id), events in grouped.items():
            if args.provider and provider != args.provider:
                skipped["other_provider"] += 1
                continue
            summary = summarize(events)
            if summary["event_count"] < args.min_events:
                skipped["below_min_events"] += 1
                continue
            if not summary["closed"] and events[-1].received_at > settled_before:
                skipped["not_settled"] += 1
                continue
            snapshots = snapshots_for(db, set(summary["checkout_ids"]))
            findings = analyze(events, snapshots=snapshots)["findings"]
            session_scoped, checkout_scoped = split_findings(findings)
            for finding in checkout_scoped:
                checkout = summary["checkout_ids"][0] if summary["checkout_ids"] else "unknown"
                checkout_findings.setdefault(
                    finding["fingerprint"], {"checkout_id": checkout, **finding}
                )
            records.append(
                {"provider": provider, "session_id": session_id, **summary}
                | {"findings": session_scoped}
            )
    with_findings = [record for record in records if record["findings"]]
    without = [record for record in records if not record["findings"]]
    # Every session that fired keeps precision measurable; a seeded draw of the
    # silent ones keeps false negatives countable without reviewing everything.
    room = max(0, args.target - len(with_findings))
    order = sorted(without, key=lambda record: (record["first_received_at"], record["session_id"]))
    drawn = random.Random(args.seed).sample(order, min(room, len(order)))
    selected = sorted(
        with_findings + drawn,
        key=lambda record: (record["first_received_at"], record["provider"], record["session_id"]),
    )
    return {
        "format_version": SAMPLE_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "project_id": str(project.id),
        "schema_version": schema_version,
        "rule_version": RULE_VERSION,
        "selection": {
            "since": args.since,
            "until": args.until,
            "provider": args.provider,
            "min_events": args.min_events,
            "settle_hours": args.settle_hours,
            "target": args.target,
            "seed": args.seed,
            "considered": len(grouped),
            "eligible": len(records),
            "skipped": dict(skipped),
            "strata": {
                "with_finding": len(with_findings),
                "without_finding_drawn": len(drawn),
                "without_finding_available": len(without),
            },
        },
        "sessions": selected,
        # Listed once: a diff oscillation belongs to a checkout, not a session.
        "checkout_findings": sorted(
            checkout_findings.values(), key=lambda finding: finding["fingerprint"]
        ),
    }


CSV_COLUMNS = (
    "provider",
    "session_id",
    "first_received_at",
    "last_received_at",
    "event_count",
    "turns",
    "tools",
    "tool_failures",
    "waiting",
    "gaps",
    "closed",
)


def write_sample(sample: dict, output: Path) -> Path:
    """Write the frozen manifest and a derived flat view for eyeballing."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([*CSV_COLUMNS, "finding_count", "rules"])
        for record in sample["sessions"]:
            writer.writerow(
                [
                    *(record[name] for name in CSV_COLUMNS),
                    len(record["findings"]),
                    " ".join(sorted({f["rule"] for f in record["findings"]})),
                ]
            )
    return csv_path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def choose(
    prompt: str, options: tuple[str, ...], hints: dict[str, str], current: str | None
) -> str | None:
    """Offer a numbered menu; an empty answer keeps the current value."""
    print(f"\n{prompt}  (Enter keeps {current or 'unset'})")
    for index, option in enumerate(options, start=1):
        marker = "*" if option == current else " "
        print(f"  {marker}{index}. {option:<20} {hints.get(option, '')}")
    answer = input("  choice > ").strip()
    if not answer:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    print("  Not a listed choice; nothing recorded.")
    return None


def one_line(value: object, width: int = 300) -> str:
    """Flatten stored content to one printable line the console can render."""
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = " ".join(text.split())
    if len(text) > width:
        text = text[: width - 3] + "..."
    return text


def provider_payload(event) -> Mapping[str, Any]:
    """Return the provider section of a stored envelope, or an empty mapping."""
    payload = event.payload.get(event.provider)
    return payload if isinstance(payload, dict) else {}


def content_of(event) -> Mapping[str, Any]:
    """Return the captured content mapping the rules read, or an empty one."""
    payload = provider_payload(event)
    return captured_content(payload) if payload else {}


def metadata_of(event) -> Mapping[str, Any]:
    metadata = provider_payload(event).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def captured_text(content: Mapping[str, Any], field: str, width: int = 300) -> tuple[str, str]:
    """Flatten one content field and say whether it was stored, empty, or absent.

    A reviewer must not read a missing capture as silence from the agent, so the
    two cases stay distinct everywhere this value is shown.
    """
    if field not in content:
        return "", "not stored"
    text = one_line(content[field], width=width)
    return (text, "stored") if text else ("", "empty")


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(max(0.0, seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def duration_note(metadata: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = metadata.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return f"{float(value) / 1000:.1f}s"
    return ""


def result_note(event, content: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[str, str]:
    """Describe a tool result as the rules read it, plus exit code, cost, and error."""
    response = content.get("tool_response")
    if "tool_response" not in content:
        outcome = "result not stored"
    elif provider_payload(event).get("hook_event_name") == "PostToolUseFailure":
        outcome = "FAILED"
    else:
        outcome = {"success": "ok", "failure": "FAILED", "unknown": "result unclear"}[
            tool_outcome(response)
        ]
    parts: list[str] = []
    code = exit_code(response)
    if code is not None:
        parts.append(f"exit {code}")
    spent = duration_note(metadata, "tool_duration_ms", "duration_ms")
    if spent:
        parts.append(spent)
    if outcome == "FAILED" and "tool_response" in content:
        parts.append(one_line(response, width=160))
    elif metadata.get("error"):
        # Without a stored result the provider's own error text is all there is.
        parts.append(f"error {one_line(metadata['error'], width=120)}")
    return outcome, "  ".join(parts)


TOOL_INPUT_KEYS = ("command", "cmd", "script", "file_path", "path", "pattern", "query", "prompt")
# Token counters carry no work a reviewer can judge; the report still counts them.
TIMELINE_SKIP_KINDS = ("usage",)


def tool_input_text(content: Mapping[str, Any], width: int = 160) -> tuple[str, str]:
    """Show the command a reviewer would recognise, not the whole argument object."""
    if "tool_input" not in content:
        return "", "not stored"
    value = content["tool_input"]
    if isinstance(value, dict):
        for key in TOOL_INPUT_KEYS:
            if value.get(key):
                return one_line(value[key], width=width), "stored"
    text = one_line(value, width=width)
    return (text, "stored") if text else ("", "empty")


def timeline_row(
    offset: float, event_id: str, marker: str, label: str, text: str, state: str
) -> dict[str, Any]:
    return {
        "offset": offset,
        "event_ids": [event_id],
        "marker": marker,
        "label": label,
        "text": text,
        "state": state,
        "note": "",
    }


def shown_state(state: str) -> str:
    """Keep the normal case quiet: only an empty or absent capture needs a marker."""
    return "" if state == "stored" else state


def build_timeline(events: list) -> list[dict]:
    """Order the session as a readable turn/tool chain instead of an event dump.

    A tool start and its finish collapse into one row, so the command, its
    result, its cost, and its error read as one link of the causal chain.
    """
    if not events:
        return []
    origin = events[0].received_at
    rows: list[dict] = []
    pending: dict[str, dict] = {}
    for event in events:
        if event.kind in TIMELINE_SKIP_KINDS:
            continue
        content = content_of(event)
        payload = provider_payload(event)
        metadata = metadata_of(event)
        offset = (event.received_at - origin).total_seconds()
        event_id = str(event.event_id)
        tool = str(payload.get("tool_name") or "tool")
        tool_use_id = payload.get("tool_use_id")
        if event.kind == "turn.start":
            text, state = captured_text(content, "prompt")
            rows.append(timeline_row(offset, event_id, ">", "prompt", text, shown_state(state)))
        elif event.kind == "turn.end":
            text, state = captured_text(content, "last_assistant_message")
            row = timeline_row(offset, event_id, "<", "reply", text, shown_state(state))
            row["note"] = duration_note(metadata, "turn_duration_ms", "duration_ms")
            rows.append(row)
        elif event.kind == "tool.start":
            text, _ = tool_input_text(content)
            row = timeline_row(offset, event_id, "$", tool, text, "no result observed")
            rows.append(row)
            if isinstance(tool_use_id, str) and tool_use_id:
                pending[tool_use_id] = row
        elif event.kind == "tool.finish":
            started = pending.pop(tool_use_id, None) if isinstance(tool_use_id, str) else None
            outcome, note = result_note(event, content, metadata)
            text, _ = tool_input_text(content)
            if started is None:
                started = timeline_row(offset, event_id, "$", tool, text, outcome)
                rows.append(started)
            else:
                started["event_ids"].append(event_id)
                started["state"] = outcome
            started["note"] = note
            if not started["text"]:
                started["text"] = text
        else:
            message = payload.get("message") or payload.get("reason") or ""
            rows.append(
                timeline_row(offset, event_id, "!", event.kind, one_line(message, width=160), "")
            )
    return rows


def timeline_line(row: Mapping[str, Any]) -> str:
    text = row["text"] or f"({row['state'] or 'no content'})"
    line = f"{clock(row['offset']):>9}  {row['marker']} {row['label']:<12} {text}"
    if row["state"] and row["text"]:
        line += f"   -> {row['state']}"
    if row["note"]:
        line += f"   {row['note']}"
    return line


CAPTURE_FIELDS = {
    "turn.start": ("prompts", "prompt"),
    "turn.end": ("replies", "last_assistant_message"),
    "tool.finish": ("results", "tool_response"),
}


def capture_status(events: list) -> dict[str, int]:
    """Count what was stored, so silence is never confused with a missing capture."""
    counts: Counter[str] = Counter()
    for event in events:
        if provider_payload(event).get("content") == "omitted":
            counts["omitted_events"] += 1
        entry = CAPTURE_FIELDS.get(event.kind)
        if entry is None:
            continue
        label, field = entry
        counts[label] += 1
        _, state = captured_text(content_of(event), field)
        counts[f"{label}_{state.replace(' ', '_')}"] += 1
    return dict(counts)


def capture_line(counts: Mapping[str, int]) -> str:
    parts: list[str] = []
    for label in ("prompts", "replies", "results"):
        total = counts.get(label, 0)
        if not total:
            continue
        detail = f"{counts.get(f'{label}_stored', 0)}/{total}"
        empty = counts.get(f"{label}_empty", 0)
        missing = counts.get(f"{label}_not_stored", 0)
        if empty:
            detail += f", {empty} empty"
        if missing:
            detail += f", {missing} not stored"
        parts.append(f"{label} {detail}")
    if counts.get("omitted_events"):
        parts.append(f"content omitted on {counts['omitted_events']} events")
    return " | ".join(parts) or "no prompt, reply, or tool result events"


def capture_is_complete(counts: Mapping[str, int]) -> bool:
    return not counts.get("omitted_events") and not any(
        counts.get(f"{label}_not_stored", 0) for label in ("prompts", "replies", "results")
    )


def session_context(paths: UserPaths, project, provider: str, session_id: str) -> dict:
    """Read what a reviewer needs to judge the session: its work, not its counts."""
    with inspection.database(paths, project) as db:
        rows = db.execute(
            "SELECT envelope FROM events WHERE session_id=? "
            "AND json_extract(envelope, '$.provider')=? ORDER BY rowid",
            (session_id, provider),
        ).fetchall()
    events = [persisted_envelope(document) for (document,) in rows]
    prompts: list[str] = []
    last_message = ""
    commands: Counter[str] = Counter()
    transcript = ""
    for event in events:
        payload = provider_payload(event)
        if isinstance(payload.get("transcript_path"), str):
            transcript = payload["transcript_path"]
        content = content_of(event)
        if event.kind == "turn.start" and content.get("prompt"):
            prompts.append(one_line(content["prompt"]))
        elif event.kind == "turn.end" and content.get("last_assistant_message"):
            last_message = one_line(content["last_assistant_message"])
        elif event.kind == "tool.start" and content.get("tool_input"):
            commands[one_line(content["tool_input"], width=120)] += 1
    timeline = build_timeline(events)
    # Recomputed only to warn that the frozen list may be stale; a verdict is
    # always recorded against the frozen sample, never against this result.
    live_findings = split_findings(analyze(events)["findings"])[0]
    return {
        "first_prompt": prompts[0] if prompts else "",
        "last_prompt": prompts[-1] if len(prompts) > 1 else "",
        "last_message": last_message,
        "repeated": [pair for pair in commands.most_common(3) if pair[1] > 1],
        "transcript_path": transcript,
        "timeline": timeline,
        "rows_by_event": {event_id: row for row in timeline for event_id in row["event_ids"]},
        "capture": capture_status(events),
        "live_findings": live_findings,
    }


def drift_line(frozen: list[dict], live: list[dict]) -> str:
    """State how a live re-analysis differs from the frozen cohort, or that it does not."""
    frozen_prints = {finding["fingerprint"] for finding in frozen}
    live_prints = {finding["fingerprint"] for finding in live}
    added = len(live_prints - frozen_prints)
    gone = len(frozen_prints - live_prints)
    if not added and not gone:
        return "live re-analysis agrees with this frozen list"
    changes = []
    if added:
        changes.append(f"{added} new since the sample")
    if gone:
        changes.append(f"{gone} no longer produced")
    return f"live re-analysis differs ({', '.join(changes)}); verdicts stay on the frozen list"


def is_reviewed(label: Mapping[str, Any]) -> bool:
    return bool(label.get("task_outcome") not in (None, "unknown") or label.get("progress_state"))


def render(record: dict, position: int, total: int, state: dict, sample: Mapping[str, Any]) -> None:
    label = state["label"]
    context = state["context"]
    print()
    print("=" * 78)
    print(
        f"[ {position}/{total} ]  {record['provider']}  {record['session_id']}   "
        f"{'reviewed' if is_reviewed(label) else 'unreviewed'}"
    )
    print(
        f"{record['first_received_at']} -> {record['last_received_at']}   "
        f"checkout {', '.join(record['checkout_ids']) or 'unknown'}"
    )
    print(
        f"{record['turns']} turns | {record['tools']} tools "
        f"({record['tool_failures']} failed) | {record['event_count']} events | "
        f"waiting {record['waiting']} | gaps {record['gaps']} | "
        f"{'closed' if record['closed'] else 'no session.end observed'}"
    )
    print(f"content {capture_line(context['capture'])}")
    if not capture_is_complete(context["capture"]):
        print("        not stored means Watchdog holds no text, not that the agent was silent")
    print()
    print(f"first prompt  > {context['first_prompt'] or '(not stored)'}")
    if context["last_prompt"]:
        print(f"last prompt   > {context['last_prompt']}")
    print(f"last message  < {context['last_message'] or '(not stored)'}")
    for command, count in context["repeated"]:
        print(f"repeated x{count}  {command}")
    tail = context["timeline"][-CARD_TAIL:]
    if tail:
        print()
        print(f"last {len(tail)} of {len(context['timeline'])} timeline rows  ([l] shows more)")
        for row in tail:
            print("  " + timeline_line(row))
    print()
    print(
        f"findings: frozen sample of {sample.get('generated_at', 'unknown')}, "
        f"rules {sample.get('rule_version', 'unknown')} - verdicts attach to these fingerprints"
    )
    if not record["findings"]:
        print("  none - a stuck or slow label here counts as a false negative")
    for index, finding in enumerate(record["findings"], start=1):
        recorded = state["verdicts"].get(finding["fingerprint"], "-")
        print(
            f"  {index}. {finding['rule']} x{finding['count']}  "
            f"{finding['fingerprint'][:12]}  verdict: {recorded}"
        )
    print(f"  {drift_line(record['findings'], context['live_findings'])}")
    print()
    print(
        f"labels  outcome={label.get('task_outcome', 'unknown')}  "
        f"type={label.get('task_type') or '-'}  "
        f"progress={label.get('progress_state') or '-'}"
    )
    if label.get("reviewer_note"):
        print(f"note    {one_line(label['reviewer_note'])}")


def show_timeline(rows: list[dict], argument: str) -> None:
    """Print the tail of the causal chain; `l all` prints the whole session."""
    if not rows:
        print("\nNo turn or tool events were stored for this session.")
        return
    if argument == "all":
        selected = rows
    else:
        count = int(argument) if argument.isdigit() and int(argument) > 0 else TIMELINE_TAIL
        selected = rows[-count:]
    print(f"\ntimeline: last {len(selected)} of {len(rows)} rows, offset from the first event")
    for row in selected:
        print("  " + timeline_line(row))


def render_detail(report: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """Print the live re-analysis in readable form.

    Hundreds of event identifiers are not something a reviewer can judge, so
    this shows the measurements and states plainly that the verdict belongs to
    the frozen sample, which this recomputation may already have outgrown.
    """
    timeline = report["timeline"]
    metrics = report["metrics"]
    outcomes = metrics["tool_outcomes"]
    print()
    print("LIVE re-analysis, not the frozen sample. Record verdicts from the card's list.")
    print(f"  rules {report['rule_version']}   events {len(report['event_ids'])}")
    print(
        f"  wall {clock(timeline['wall_seconds'])}   "
        f"active {clock(timeline['active_seconds'])}"
        f"{'' if timeline['active_seconds'] is not None else ' (unmatched turn boundary)'}"
    )
    print(
        f"  tools ok {outcomes['success']} | failed {outcomes['failure']} | "
        f"unclear {outcomes['unknown']}   "
        f"slowest {clock(metrics['tool_durations']['max_seconds'])}"
    )
    print(
        "  event kinds "
        + ", ".join(f"{kind} {count}" for kind, count in timeline["event_counts"].items())
    )
    deltas = metrics["usage"]["deltas"]
    print(
        "  usage "
        + (", ".join(f"{name} {value}" for name, value in deltas.items()) or "not observed")
    )
    print("  gaps " + (", ".join(report["gaps"]) or "none"))
    live_session, live_checkout = split_findings(list(report["findings"]))
    live_prints = {finding["fingerprint"] for finding in live_session}
    frozen_prints = {finding["fingerprint"] for finding in record["findings"]}
    print(f"  live session-scoped findings {len(live_session)}:")
    for finding in live_session:
        origin = "in the frozen sample" if finding["fingerprint"] in frozen_prints else "NEW"
        print(f"    {finding['rule']} x{finding['count']}  {finding['fingerprint'][:12]}  {origin}")
    for finding in record["findings"]:
        if finding["fingerprint"] not in live_prints:
            print(
                f"    {finding['rule']} x{finding['count']}  "
                f"{finding['fingerprint'][:12]}  frozen only, no longer produced"
            )
    print(
        f"  live checkout-scoped findings {len(live_checkout)} - reviewed once under [c] "
        "from the frozen sample, not from this count"
    )
    print(f"  recorded verdicts {len(report['verdicts'])}")


def open_observed_path(path: Path) -> None:
    """Hand a transcript to the system viewer at the reviewer's request.

    The path comes from an untrusted trace, so only an existing text-shaped file
    is handed over; Watchdog never launches whatever a trace happens to name and
    never writes to a vendor transcript.
    """
    if not path.is_file():
        print("  The observed path no longer exists.")
        return
    if path.suffix.lower() not in TRANSCRIPT_SUFFIXES:
        print(f"  Refusing to open '{path.suffix or 'no suffix'}' named by an observed trace.")
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    except OSError as error:
        print(f"  Could not open it: {error}")


def offer_transcript(path: str) -> None:
    """Point at the vendor transcript, the full record behind this triage."""
    if not path:
        print("\nNo transcript path was observed for this session.")
        return
    print(f"\n{path}")
    print("  The vendor transcript is the full record when card and timeline do not settle it.")
    if input("  open it with the system viewer? [y/N] > ").strip().lower() in ("y", "yes"):
        open_observed_path(Path(path))


def first_unreviewed(paths: UserPaths, project, records: list[dict]) -> int:
    """Resume at the first unlabelled session instead of restarting the cohort."""
    with inspection.database(paths, project) as db:
        for index, record in enumerate(records):
            label = inspection.session_label(db, record["provider"], record["session_id"])
            if not is_reviewed(label):
                return index
    return 0


def load_state(paths: UserPaths, project, provider: str, session_id: str) -> dict:
    with inspection.database(paths, project) as db:
        label = dict(inspection.session_label(db, provider, session_id))
        verdicts = {
            row["fingerprint"]: row["verdict"]
            for row in inspection.session_verdicts(db, provider, session_id)
        }
    context = session_context(paths, project, provider, session_id)
    return {"label": label, "verdicts": verdicts, "context": context}


def submit(paths: UserPaths, request: dict) -> bool:
    """Send one annotation to the core and report whether it was acknowledged."""
    try:
        result = daemon.request_control(paths, request)
    except Exception as error:  # noqa: BLE001 - the reviewer needs the reason, not a trace
        print(f"  NOT recorded: {error}")
        return False
    if not result.get("ok", True):
        print(f"  NOT recorded: {result.get('message')}")
        return False
    print("  recorded")
    return True


def label_request(project_id: str, record: dict, **fields) -> dict:
    return {
        "action": "label",
        "project_id": project_id,
        "provider": record["provider"],
        "session_id": record["session_id"],
        "outcome": fields.pop("outcome"),
        "task_type": fields.pop("task_type", None),
        "progress_state": fields.pop("progress_state", None),
        "reviewer_note": fields.pop("reviewer_note", None),
    }


def print_evidence(finding: Mapping[str, Any], rows_by_event: Mapping[str, dict]) -> None:
    """Show evidence as work a reviewer can read, not as event identifiers."""
    evidence_ids = list(finding["evidence_ids"])
    print("evidence")
    for event_id in evidence_ids[:EVIDENCE_SNIPPETS]:
        row = rows_by_event.get(event_id)
        if row is None:
            print(f"  {event_id[:8]}  not in the current store")
        else:
            print("  " + timeline_line(row))
    if len(evidence_ids) > EVIDENCE_SNIPPETS:
        print(f"  ... {len(evidence_ids) - EVIDENCE_SNIPPETS} more of the same group")


def annotate_finding(
    paths: UserPaths, project_id: str, record: dict, finding: dict, state: dict
) -> None:
    print()
    print(f"{finding['rule']} x{finding['count']}  {finding['fingerprint'][:12]}")
    print(f"rule says: {finding['explanation']}")
    print_evidence(finding, state["context"]["rows_by_event"])
    verdict = choose("Finding verdict", FINDING_VERDICTS, VERDICT_HINTS, None)
    if verdict is None:
        return
    note = input("  note (optional) > ").strip() or None
    submit(
        paths,
        {
            "action": "verdict",
            "project_id": project_id,
            "provider": record["provider"],
            "session_id": record["session_id"],
            "rule": finding["rule"],
            "rule_version": finding["rule_version"],
            "fingerprint": finding["fingerprint"],
            "verdict": verdict,
            "note": note,
        },
    )


def annotate_checkout_findings(paths: UserPaths, project_id: str, findings: list[dict]) -> None:
    """Review checkout-scoped findings once instead of once per session."""
    if not findings:
        print("\nNo checkout-scoped findings in this sample.")
        return
    print(f"\nCheckout-scoped findings frozen in this sample: {len(findings)}.")
    print("A live report can show a different count; the verdict belongs to the frozen set.")
    for index, finding in enumerate(findings, start=1):
        print()
        print("-" * 78)
        print(
            f"[ {index}/{len(findings)} ]  {finding['rule']} x{finding['count']}  "
            f"checkout {finding['checkout_id']}"
        )
        print(f"rule says: {finding['explanation']}")
        print(f"attribution: {finding['attribution']}")
        # Diff-oscillation evidence is a chain of diff snapshots, not session events.
        print("evidence: diff snapshots " + ", ".join(item[:8] for item in finding["evidence_ids"]))
        verdict = choose("Finding verdict", FINDING_VERDICTS, VERDICT_HINTS, None)
        if verdict is None:
            continue
        note = input("  note (optional) > ").strip() or None
        submit(
            paths,
            {
                "action": "checkout_verdict",
                "project_id": project_id,
                "checkout_id": finding["checkout_id"],
                "rule": finding["rule"],
                "rule_version": finding["rule_version"],
                "fingerprint": finding["fingerprint"],
                "verdict": verdict,
                "note": note,
            },
        )


HELP = (
    "[o]utcome [t]ype [p]rogress [r]emark [1-9] verdict | "
    "[l]ine timeline (l 50 | l all) [d]etail live report [dj] raw JSON | "
    "e[x]ternal transcript [c]heckout findings | [n]ext [b]ack [j]ump [q]uit"
)


def annotate(paths: UserPaths, args: argparse.Namespace) -> int:
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    if sample.get("format_version") != SAMPLE_FORMAT:
        print(f"Unsupported sample format: {sample.get('format_version')}", file=sys.stderr)
        return 1
    project = inspection.project_at(paths, args.project)
    if str(project.id) != sample["project_id"]:
        print("The sample was taken from a different project", file=sys.stderr)
        return 1
    records = sample["sessions"]
    if not records:
        print("The sample contains no sessions.")
        return 0
    position = first_unreviewed(paths, project, records)
    print(f"Resuming at session {position + 1} of {len(records)}.")
    while True:
        record = records[position]
        state = load_state(paths, project, record["provider"], record["session_id"])
        render(record, position + 1, len(records), state, sample)
        print(HELP)
        try:
            entered = input("> ").strip().lower().split()
        except EOFError:
            return 0
        command = entered[0] if entered else ""
        argument = entered[1] if len(entered) > 1 else ""
        if command in ("q", "quit"):
            return 0
        if command in ("n", "next", ""):
            position = min(position + 1, len(records) - 1)
        elif command in ("b", "back"):
            position = max(position - 1, 0)
        elif command in ("j", "jump"):
            answer = argument or input(f"  session number 1..{len(records)} > ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(records):
                position = int(answer) - 1
        elif command in ("l", "line", "timeline"):
            show_timeline(state["context"]["timeline"], argument)
        elif command in ("d", "detail", "dj"):
            report = inspection.report(
                paths, project, session_id=record["session_id"], provider=record["provider"]
            )
            if command == "dj":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                render_detail(report, record)
        elif command in ("x", "transcript"):
            offer_transcript(state["context"]["transcript_path"])
        elif command in ("c", "checkout"):
            annotate_checkout_findings(paths, str(project.id), sample["checkout_findings"])
        elif command in ("o", "outcome"):
            outcome = choose("Task outcome", TASK_OUTCOMES, {}, state["label"]["task_outcome"])
            if outcome is not None:
                submit(paths, label_request(str(project.id), record, outcome=outcome))
        elif command in ("t", "type"):
            task_type = input("  task type (free text) > ").strip()
            if task_type:
                submit(
                    paths,
                    label_request(
                        str(project.id),
                        record,
                        outcome=state["label"]["task_outcome"],
                        task_type=task_type,
                    ),
                )
        elif command in ("p", "progress"):
            progress = choose(
                "Progress state", PROGRESS_STATES, PROGRESS_HINTS, state["label"]["progress_state"]
            )
            if progress is not None:
                submit(
                    paths,
                    label_request(
                        str(project.id),
                        record,
                        outcome=state["label"]["task_outcome"],
                        progress_state=progress,
                    ),
                )
        elif command in ("r", "remark"):
            note = input("  reviewer note > ").strip()
            if note:
                submit(
                    paths,
                    label_request(
                        str(project.id),
                        record,
                        outcome=state["label"]["task_outcome"],
                        reviewer_note=note,
                    ),
                )
        elif command.isdigit() and 1 <= int(command) <= len(record["findings"]):
            annotate_finding(
                paths, str(project.id), record, record["findings"][int(command) - 1], state
            )
        else:
            print("Unknown command.")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return round(ordered[index], 1)


def overhead(paths: UserPaths, project) -> dict:
    """Measure hook cost from the delivery trace already stored on envelopes."""
    in_hook: list[float] = []
    end_to_end: list[float] = []
    missing = 0
    total = 0
    with inspection.database(paths, project) as db:
        for (document,) in db.execute("SELECT envelope FROM events"):
            total += 1
            delivery = json.loads(document).get("delivery") or {}
            try:
                started = parse_time(delivery["adapter_started_at"])
                spooled = parse_time(delivery["spool_enqueued_at"])
                written = parse_time(delivery["sqlite_write_started_at"])
            except KeyError:
                missing += 1
                continue
            assert started is not None and spooled is not None and written is not None
            in_hook.append((spooled - started).total_seconds() * 1000)
            end_to_end.append((written - started).total_seconds() * 1000)
    return {
        "events": total,
        "events_with_delivery": len(in_hook),
        "events_without_delivery": missing,
        "in_hook_ms": {
            "p50": percentile(in_hook, 0.5),
            "p95": percentile(in_hook, 0.95),
            "max": round(max(in_hook), 1) if in_hook else None,
            "mean": round(statistics.fmean(in_hook), 1) if in_hook else None,
        },
        "end_to_end_ms": {
            "p50": percentile(end_to_end, 0.5),
            "p95": percentile(end_to_end, 0.95),
            "max": round(max(end_to_end), 1) if end_to_end else None,
        },
        "note": (
            "Transcript-sourced events carry no delivery trace, so the denominator "
            "is smaller than the event count. Null is not zero."
        ),
    }


def rule_precision(rules: tuple[str, ...], observed: Counter, verdicts: Counter) -> dict:
    """Precision per rule; a rule with no observation stays null, never 100%."""
    result: dict[str, Any] = {}
    for rule in sorted(set(rules) | set(observed) | {rule for rule, _ in verdicts}):
        true_positive = verdicts[(rule, "true_positive")]
        false_positive = verdicts[(rule, "false_positive")]
        uncertain = verdicts[(rule, "uncertain")]
        decided = true_positive + false_positive
        result[rule] = {
            "observed": observed[rule],
            "reviewed": decided + uncertain,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "uncertain": uncertain,
            "precision": round(true_positive / decided, 4) if decided else None,
            "precision_denominator": decided,
        }
        if not observed[rule]:
            result[rule]["reason"] = "no observation in this dataset; null is not zero"
        elif not decided:
            result[rule]["reason"] = "observed but not reviewed; precision is unmeasured"
    return result


def build_report(paths: UserPaths, args: argparse.Namespace) -> dict:
    sample_path = Path(args.sample)
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    project = inspection.project_at(paths, args.project)
    observed: Counter[str] = Counter()
    verdicts: Counter[tuple[str, str]] = Counter()
    states: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    confusion: Counter[tuple[str, str]] = Counter()
    false_negatives: list[dict] = []
    unreviewed: list[str] = []
    with inspection.database(paths, project) as db:
        for record in sample["sessions"]:
            label = inspection.session_label(db, record["provider"], record["session_id"])
            recorded = {
                row["fingerprint"]: row["verdict"]
                for row in inspection.session_verdicts(db, record["provider"], record["session_id"])
            }
            progress = label["progress_state"]
            states[progress or "unlabeled"] += 1
            outcomes[label["task_outcome"]] += 1
            fired = "finding" if record["findings"] else "no_finding"
            confusion[(progress or "unlabeled", fired)] += 1
            if progress is None:
                unreviewed.append(record["session_id"])
            if progress in ("slow", "stuck") and not record["findings"]:
                false_negatives.append(
                    {
                        "provider": record["provider"],
                        "session_id": record["session_id"],
                        "progress_state": progress,
                        "reviewer_note": label["reviewer_note"],
                    }
                )
            for finding in record["findings"]:
                observed[finding["rule"]] += 1
                verdict = recorded.get(finding["fingerprint"])
                if verdict is not None:
                    verdicts[(finding["rule"], verdict)] += 1
        checkout = {row["fingerprint"]: row for row in inspection.checkout_verdicts(db)}
    checkout_observed: Counter[str] = Counter()
    checkout_verdicts_seen: Counter[tuple[str, str]] = Counter()
    for finding in sample["checkout_findings"]:
        checkout_observed[finding["rule"]] += 1
        recorded_row = checkout.get(finding["fingerprint"])
        if recorded_row is not None:
            checkout_verdicts_seen[(finding["rule"], recorded_row["verdict"])] += 1
    session_rules = tuple(rule for rule in RULES if rule not in CHECKOUT_SCOPED_RULES)
    session_stats = rule_precision(session_rules, observed, verdicts)
    checkout_stats = rule_precision(
        CHECKOUT_SCOPED_RULES, checkout_observed, checkout_verdicts_seen
    )
    unmeasured = [
        rule
        for rule, stats in list(session_stats.items()) + list(checkout_stats.items())
        if not stats["observed"]
    ]
    return {
        "format_version": CALIBRATION_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "supersedes": {
            "format_version": "wd-012.calibration.v1",
            "reason": (
                "The first pass read local Codex rollout files directly, so no Watchdog "
                "report or per-hook timing existed and every measurement was null."
            ),
        },
        "dataset": {
            "sample_file": sample_path.name,
            "sample_sha256": digest(sample_path),
            "generated_at": sample["generated_at"],
            "project_id": sample["project_id"],
            "schema_version": sample["schema_version"],
            "rule_version": sample["rule_version"],
            "selection": sample["selection"],
            "sessions": len(sample["sessions"]),
        },
        "session_states": dict(states),
        "task_outcomes": dict(outcomes),
        "confusion": {f"{state}/{fired}": count for (state, fired), count in confusion.items()},
        "session_scoped_rules": session_stats,
        "checkout_scoped_rules": {
            "note": (
                "Diff snapshots are keyed by checkout, so these findings are not "
                "attributable to one session and are excluded from session precision."
            ),
            "rules": checkout_stats,
        },
        "false_negatives": {
            "definition": "sessions judged slow or stuck with no session-scoped finding",
            "count": len(false_negatives),
            "sessions": false_negatives,
        },
        "unreviewed_sessions": unreviewed,
        "hook_overhead": overhead(paths, project),
        "limitations": [
            f"Rules with no observation in this dataset: {', '.join(unmeasured) or 'none'}.",
            "Checkout-scoped findings are excluded from per-session precision.",
            "One operator labelled their own sessions; the judgement is not independent.",
            "The session count is a calibration target, not proof of representativeness.",
        ],
    }


def render_markdown(report: dict) -> str:
    dataset = report["dataset"]
    lines = [
        "# WD-012 calibration report",
        "",
        f"Generated {report['generated_at']} from `{dataset['sample_file']}` "
        f"(sha256 `{dataset['sample_sha256']}`).",
        "",
        "## Dataset",
        "",
        f"- Sessions: {dataset['sessions']}",
        f"- Rule version: {dataset['rule_version']}",
        f"- Storage schema: v{dataset['schema_version']}",
        f"- Selection: {json.dumps(dataset['selection'], sort_keys=True)}",
        "",
        "## Session states",
        "",
    ]
    for state, count in sorted(report["session_states"].items()):
        lines.append(f"- {state}: {count}")
    lines += ["", "## Session-scoped rule precision", ""]
    lines.append("| rule | observed | reviewed | TP | FP | uncertain | precision |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for rule, stats in report["session_scoped_rules"].items():
        precision = "null" if stats["precision"] is None else f"{stats['precision']:.2%}"
        lines.append(
            f"| {rule} | {stats['observed']} | {stats['reviewed']} | {stats['true_positive']} "
            f"| {stats['false_positive']} | {stats['uncertain']} | {precision} |"
        )
    lines += [
        "",
        "## False negatives",
        "",
        f"{report['false_negatives']['count']} ({report['false_negatives']['definition']}).",
        "",
        "## Hook overhead",
        "",
        f"- In-hook p50/p95: {report['hook_overhead']['in_hook_ms']['p50']} / "
        f"{report['hook_overhead']['in_hook_ms']['p95']} ms",
        f"- End-to-end p50/p95: {report['hook_overhead']['end_to_end_ms']['p50']} / "
        f"{report['hook_overhead']['end_to_end_ms']['p95']} ms",
        f"- Measured on {report['hook_overhead']['events_with_delivery']} of "
        f"{report['hook_overhead']['events']} events. "
        f"{report['hook_overhead']['note']}",
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="Isolated home holding config.toml, data and runtime")
    parser.add_argument("--config")
    parser.add_argument("--data")
    parser.add_argument("--runtime")
    parser.add_argument("--project", help="Project alias; UUID accepted; default resolves cwd")


def paths_from(args: argparse.Namespace) -> UserPaths:
    """Resolve the same config/data/runtime overrides the CLI accepts."""
    paths = user_paths()
    if args.home:
        home = Path(args.home).resolve()
        paths = UserPaths(home / "config.toml", home / "data", home / "runtime")
    return UserPaths(
        Path(args.config).resolve() if args.config else paths.config,
        Path(args.data).resolve() if args.data else paths.data,
        Path(args.runtime).resolve() if args.runtime else paths.runtime,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample", help="Freeze a reproducible calibration cohort")
    add_common(sample)
    sample.add_argument("--since", help="ISO timestamp; earlier events are ignored")
    sample.add_argument("--until", help="ISO timestamp; later events are ignored")
    sample.add_argument("--provider", choices=("codex", "claude"))
    sample.add_argument("--min-events", type=int, default=20)
    sample.add_argument("--settle-hours", type=float, default=2.0)
    sample.add_argument("--target", type=int, default=40)
    sample.add_argument("--seed", type=int, default=12)
    sample.add_argument("--output", default="docs/evidence/wd012-sample.json")

    review = commands.add_parser("annotate", help="Review the frozen cohort interactively")
    add_common(review)
    review.add_argument("--sample", default="docs/evidence/wd012-sample.json")

    result = commands.add_parser("report", help="Emit the calibration evidence")
    add_common(result)
    result.add_argument("--sample", default="docs/evidence/wd012-sample.json")
    result.add_argument("--output", default="docs/evidence/wd012-calibration.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Reviewed prompts and messages are the reviewer's own text in any language;
    # a legacy console code page would replace them with question marks.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    paths = paths_from(args)
    if args.command == "sample":
        sample = build_sample(paths, args)
        output = Path(args.output)
        csv_path = write_sample(sample, output)
        print(
            json.dumps(
                {
                    "sample": str(output),
                    "csv": str(csv_path),
                    "sessions": len(sample["sessions"]),
                    "checkout_findings": len(sample["checkout_findings"]),
                    "selection": sample["selection"],
                }
            )
        )
        return 0
    if args.command == "annotate":
        return annotate(paths, args)
    report = build_report(paths, args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"report": str(output), "markdown": str(markdown)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
