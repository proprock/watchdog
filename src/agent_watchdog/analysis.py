"""Read-only, deterministic shadow analysis for collected observation events."""

import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_watchdog._proc import run as _run
from agent_watchdog.events import Envelope

REPORT_SCHEMA_VERSION = 1
RULE_VERSION = "wd-010.v1"
REPETITION_THRESHOLD = 3
_PYTEST_FAILURE = re.compile(r"^FAILED\s+([^\s]+)", re.MULTILINE)
_JUNIT_FAILURE = re.compile(
    r"<testcase\b[^>]*(?:classname=[\"']([^\"']*)[\"'])?[^>]*"
    r"(?:name=[\"']([^\"']*)[\"'])?[^>]*>\s*<(?:failure|error)\b",
    re.IGNORECASE,
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _provider(event: Envelope) -> Mapping[str, Any]:
    value = event.payload.get(event.provider)
    return value if isinstance(value, dict) else {}


def _captured(provider: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return content when retained, with direct fixtures accepted for analysis."""
    content = provider.get("content")
    if isinstance(content, dict):
        return content
    return provider


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _exit_code(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ("exit_code", "exitCode"):
        code = value.get(key)
        if type(code) is int:
            return code
    return None


def _outcome(response: object) -> str:
    code = _exit_code(response)
    if code is not None:
        return "success" if code == 0 else "failure"
    if isinstance(response, dict) and response.get("isError") is True:
        return "failure"
    return "unknown"


def _test_failures(command: object, response: object) -> tuple[str, ...]:
    text = "\n".join(_strings(response))
    command_text = "\n".join(_strings(command)).lower()
    pytest = set(_PYTEST_FAILURE.findall(text)) if "pytest" in command_text else set()
    junit = {
        "::".join(part for part in pair if part)
        for pair in _JUNIT_FAILURE.findall(text)
        if any(pair)
    }
    return tuple(sorted(pytest | junit))


def _finding(
    rule: str, evidence_ids: list[str], explanation: str, *, attribution: str = "observed"
) -> dict[str, Any]:
    return {
        "rule": rule,
        "rule_version": RULE_VERSION,
        "evidence_ids": evidence_ids,
        "count": len(evidence_ids),
        "attribution": attribution,
        "explanation": explanation,
    }


def git_diff_fingerprint(checkout: Path) -> tuple[str, int] | None:
    """Hash an unmodified checkout diff; failures remain unknown, not clean."""
    try:
        result = _run(
            [
                "git",
                "-C",
                str(checkout),
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--",
            ],
            capture_output=True,
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest(), len(result.stdout)


def diff_oscillations(snapshots: Iterable[Mapping[str, object]]) -> list[dict[str, Any]]:
    """Return A-to-B-to-A fingerprint signals, never ownership claims."""
    by_checkout: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for snapshot in snapshots:
        checkout = snapshot.get("checkout_id")
        fingerprint = snapshot.get("fingerprint")
        snapshot_id = snapshot.get("snapshot_id")
        if not all(isinstance(item, str) and item for item in (checkout, fingerprint, snapshot_id)):
            continue
        by_checkout[str(checkout)].append(snapshot)
    findings = []
    for entries in by_checkout.values():
        for first, second, third in zip(entries, entries[1:], entries[2:], strict=False):
            if first["fingerprint"] == third["fingerprint"] != second["fingerprint"]:
                findings.append(
                    _finding(
                        "diff_oscillation",
                        [
                            str(first["snapshot_id"]),
                            str(second["snapshot_id"]),
                            str(third["snapshot_id"]),
                        ],
                        "Observed A-to-B-to-A Git diff fingerprints; "
                        "concurrent edits are not attributable.",
                        attribution="uncertain",
                    )
                )
    return findings


def analyze(
    events: Iterable[Envelope], *, snapshots: Iterable[Mapping[str, object]] = ()
) -> dict[str, Any]:
    """Build a reproducible report from persisted envelopes and diff fingerprints.

    Missing observations are surfaced as gaps.  In particular, this function
    intentionally does not infer a stall or a successful task outcome.
    """
    ordered = sorted(events, key=lambda event: (event.received_at, str(event.event_id)))
    kinds = Counter(event.kind for event in ordered)
    event_ids = [str(event.event_id) for event in ordered]
    sessions = sorted({event.session_id for event in ordered if event.session_id is not None})
    first = ordered[0].received_at if ordered else None
    last = ordered[-1].received_at if ordered else None
    wall_seconds = (
        (last - first).total_seconds() if first is not None and last is not None else None
    )

    turns: dict[tuple[str, str], datetime] = {}
    active_seconds = 0.0
    unmatched_turn_boundary = False
    for event in ordered:
        if event.session_id is None:
            continue
        key = (event.provider, event.session_id)
        if event.kind == "turn.start":
            if key in turns:
                unmatched_turn_boundary = True
            turns[key] = event.received_at
        elif event.kind == "turn.end":
            start = turns.pop(key, None)
            if start is None:
                unmatched_turn_boundary = True
            else:
                active_seconds += max(0.0, (event.received_at - start).total_seconds())
    if turns:
        unmatched_turn_boundary = True
    active = None if unmatched_turn_boundary else active_seconds

    tool_starts: dict[tuple[str, str, str], datetime] = {}
    tool_durations: list[float] = []
    for event in ordered:
        if event.session_id is None:
            continue
        tool_use_id = _provider(event).get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            continue
        key = (event.provider, event.session_id, tool_use_id)
        if event.kind == "tool.start":
            tool_starts[key] = event.received_at
        elif event.kind == "tool.finish":
            started = tool_starts.pop(key, None)
            if started is not None:
                tool_durations.append(max(0.0, (event.received_at - started).total_seconds()))

    outcomes: Counter[str] = Counter()
    output_bytes = 0
    output_available = 0
    repeated: dict[str, list[str]] = defaultdict(list)
    errors: dict[str, list[str]] = defaultdict(list)
    failing_tests: dict[str, list[str]] = defaultdict(list)
    usage: dict[str, int] = defaultdict(int)
    usage_records = 0
    usage_unavailable = False
    telemetry_fields: Counter[str] = Counter()
    unknown_fields: Counter[str] = Counter()
    for event in ordered:
        provider = _provider(event)
        metadata = provider.get("metadata")
        if isinstance(metadata, dict):
            telemetry_fields.update(key for key in metadata if isinstance(key, str))
        unknown = provider.get("unknown_fields")
        if isinstance(unknown, list):
            unknown_fields.update(item for item in unknown if isinstance(item, str))
        if event.kind == "usage":
            record = provider.get("usage")
            delta = record.get("delta") if isinstance(record, dict) else None
            if not isinstance(delta, dict):
                usage_unavailable = True
            else:
                usage_records += 1
                for key, value in delta.items():
                    if type(value) is int and value >= 0:
                        usage[key] += value
                    elif value is None:
                        usage_unavailable = True
            continue
        if event.kind != "tool.finish":
            continue
        captured = _captured(provider)
        command = captured.get("tool_input")
        response = captured.get("tool_response")
        if response is None:
            outcomes["unknown"] += 1
            continue
        output_available += 1
        output_bytes += len(_canonical(response).encode("utf-8"))
        outcome = _outcome(response)
        outcomes[outcome] += 1
        tool = provider.get("tool_name")
        signature = _fingerprint({"tool": tool, "input": command, "outcome": outcome})
        repeated[signature].append(str(event.event_id))
        if outcome != "failure":
            continue
        error_signature = _fingerprint({"code": _exit_code(response), "response": response})
        errors[error_signature].append(str(event.event_id))
        tests = _test_failures(command, response)
        if tests:
            test_signature = _fingerprint({"input": command, "failures": tests})
            failing_tests[test_signature].append(str(event.event_id))

    findings: list[dict[str, Any]] = []
    for evidence_ids in repeated.values():
        if len(evidence_ids) >= REPETITION_THRESHOLD:
            findings.append(
                _finding(
                    "repeated_tool_outcome",
                    evidence_ids,
                    "Observed the same tool input and outcome at least three times.",
                )
            )
    for evidence_ids in errors.values():
        if len(evidence_ids) >= REPETITION_THRESHOLD:
            findings.append(
                _finding(
                    "identical_error",
                    evidence_ids,
                    "Observed the same structured failing tool result at least three times.",
                )
            )
    for evidence_ids in failing_tests.values():
        if len(evidence_ids) >= REPETITION_THRESHOLD:
            findings.append(
                _finding(
                    "repeated_test_failure",
                    evidence_ids,
                    "Observed the same failing pytest/JUnit set for a matching test command "
                    "at least three times.",
                )
            )
    findings.extend(diff_oscillations(snapshots))
    findings.sort(key=lambda finding: (str(finding["rule"]), list(finding["evidence_ids"])))

    gaps = ["task_outcome_unknown"]
    if active is None:
        gaps.insert(0, "active_time_unknown")
    if output_available < kinds["tool.finish"]:
        gaps.append("tool_output_unavailable")
    if not usage_records or usage_unavailable:
        gaps.append("usage_incomplete")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "event_ids": event_ids,
        "session_ids": sessions,
        "timeline": {
            "first_received_at": first.isoformat() if first is not None else None,
            "last_received_at": last.isoformat() if last is not None else None,
            "wall_seconds": wall_seconds,
            "active_seconds": active,
            "event_counts": dict(sorted(kinds.items())),
        },
        "metrics": {
            "tool_outcomes": {name: outcomes[name] for name in ("failure", "success", "unknown")},
            "tool_durations": {
                "observed_count": len(tool_durations),
                "total_seconds": sum(tool_durations) if tool_durations else None,
                "max_seconds": max(tool_durations) if tool_durations else None,
            },
            "output_bytes": output_bytes if output_available else None,
            "compactions": {
                "started": kinds["compaction.start"],
                "completed": kinds["compaction.end"],
            },
            "usage": {"records": usage_records, "deltas": dict(sorted(usage.items()))},
            "telemetry": {
                "observed_fields": dict(sorted(telemetry_fields.items())),
                "unknown_fields": dict(sorted(unknown_fields.items())),
            },
        },
        "findings": findings,
        "gaps": gaps,
    }
