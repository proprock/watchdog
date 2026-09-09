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
import random
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_watchdog import daemon, inspection
from agent_watchdog.analysis import RULE_VERSION, RULES, analyze
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


def render(record: dict, position: int, total: int, state: dict) -> None:
    label = state["label"]
    reviewed = label.get("task_outcome", "unknown") != "unknown"
    print()
    print("=" * 78)
    print(
        f"[ {position}/{total} ]  {record['provider']}  {record['session_id']}   "
        f"{'reviewed' if reviewed else 'unreviewed'}"
    )
    print(
        f"{record['first_received_at']} -> {record['last_received_at']}   "
        f"checkout {', '.join(record['checkout_ids']) or 'unknown'}"
    )
    print(
        f"{record['turns']} turns · {record['tools']} tools "
        f"({record['tool_failures']} failed) · {record['event_count']} events · "
        f"waiting {record['waiting']} · gaps {record['gaps']} · "
        f"{'closed' if record['closed'] else 'no session.end observed'}"
    )
    print()
    print("findings (session-scoped)")
    if not record["findings"]:
        print("  none — a stuck or slow label here counts as a false negative")
    for index, finding in enumerate(record["findings"], start=1):
        recorded = state["verdicts"].get(finding["fingerprint"], "-")
        print(
            f"  {index}. {finding['rule']} x{finding['count']}  "
            f"{finding['fingerprint'][:12]}  verdict: {recorded}"
        )
    print()
    print(
        f"labels  outcome={label.get('task_outcome', 'unknown')}  "
        f"type={label.get('task_type') or '-'}  "
        f"progress={label.get('progress_state') or '-'}"
    )


def load_state(paths: UserPaths, project, provider: str, session_id: str) -> dict:
    with inspection.database(paths, project) as db:
        label = dict(inspection.session_label(db, provider, session_id))
        verdicts = {
            row["fingerprint"]: row["verdict"]
            for row in inspection.session_verdicts(db, provider, session_id)
        }
    return {"label": label, "verdicts": verdicts}


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


def annotate_finding(paths: UserPaths, project_id: str, record: dict, finding: dict) -> None:
    print(f"\nevidence: {', '.join(finding['evidence_ids'])}")
    print(f"explanation: {finding['explanation']}")
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
    print(f"\nCheckout-scoped findings: {len(findings)}")
    for index, finding in enumerate(findings, start=1):
        print()
        print("-" * 78)
        print(
            f"[ {index}/{len(findings)} ]  {finding['rule']} x{finding['count']}  "
            f"checkout {finding['checkout_id']}"
        )
        print(f"evidence: {', '.join(finding['evidence_ids'])}")
        print(f"attribution: {finding['attribution']} — {finding['explanation']}")
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
    "[o]utcome [t]ype [p]rogress [r]emark [1-9] verdict "
    "[d]etail [c]heckout findings [n]ext [b]ack [j]ump [q]uit"
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
    position = 0
    while True:
        record = records[position]
        state = load_state(paths, project, record["provider"], record["session_id"])
        render(record, position + 1, len(records), state)
        print(HELP)
        try:
            command = input("> ").strip().lower()
        except EOFError:
            return 0
        if command in ("q", "quit"):
            return 0
        if command in ("n", "next", ""):
            position = min(position + 1, len(records) - 1)
        elif command in ("b", "back"):
            position = max(position - 1, 0)
        elif command in ("j", "jump"):
            answer = input(f"  session number 1..{len(records)} > ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(records):
                position = int(answer) - 1
        elif command in ("d", "detail"):
            report = inspection.report(
                paths, project, session_id=record["session_id"], provider=record["provider"]
            )
            print(json.dumps(report, indent=2, sort_keys=True))
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
            annotate_finding(paths, str(project.id), record, record["findings"][int(command) - 1])
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
