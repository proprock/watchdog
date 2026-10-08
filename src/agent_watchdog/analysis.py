"""Read-only, deterministic shadow analysis for collected observation events."""

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agent_watchdog._proc import run as _run
from agent_watchdog.events import Envelope
from agent_watchdog.facts import USAGE_COLUMNS, claude_response_usage

REPORT_SCHEMA_VERSION = 3
# v2 (WD-146): Claude outcomes come from the failure/success hooks, repeats must
# be edit-free or produce the same output, error text is normalised, and a diff
# oscillation needs an edit or turn start between its snapshots.
RULE_VERSION = "wd-010.v2"
# Every rule `analyze` can emit. A calibration report must list a rule that
# never fired as unmeasured rather than omitting it.
RULES = (
    "repeated_tool_outcome",
    "identical_error",
    "repeated_test_failure",
    "diff_oscillation",
)
REPETITION_THRESHOLD = 3
# Tools that change files.  Codex edits through `apply_patch`.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
# Paths, quoted values, hex ids and numbers differ between runs of one failure.
_ERROR_NORMALIZERS = (
    (re.compile(r"[A-Za-z]:[\\/][^\s'\":]*"), "<path>"),
    (re.compile(r"(?<![\w.<>])/(?:[^\s/'\":]+/)+[^\s'\":]*"), "<path>"),
    (re.compile(r"'[^']*'"), "'<s>'"),
    (re.compile(r"\"[^\"]*\""), '"<s>"'),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
)
_EXIT_CODE_TEXT = re.compile(r"\AExit code (-?\d+)\b")
# WD-121 checkout fingerprint bounds; a state beyond any of them is unknown.
# The tag domain-separates it from the v1 hash of unstaged tracked changes only.
_CHECKOUT_FINGERPRINT_TAG = b"v2-checkout\0"
CHECKOUT_DEADLINE_SECONDS = 1.5
MAX_DIFF_BYTES = 16 * 1024 * 1024
MAX_UNTRACKED_FILES = 500
MAX_UNTRACKED_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
# The WD-014 deterministic policy-rule class: conditions are structural facts,
# not statistical judgments, so these are exempt from the WD-013 precision
# gate and carry their own rule/version namespace and an explicit `action`.
POLICY_RULE_VERSION = "wd-014.v1"
POLICY_RULES = ("same_model_subagent_spawn",)
# The counters the claude-transcript-v1 reader records as observed or
# unavailable; cache writes and thinking tokens are optional per response.
_CLAUDE_REQUIRED_USAGE = ("input_tokens", "cached_input_tokens", "output_tokens")
TELEMETRY_SCHEMA_VERSION = 1
_DELIVERY_TIMESTAMPS = (
    "adapter_started_at",
    "spool_enqueued_at",
    "spool_drained_at",
    "inbox_enqueued_at",
    "inbox_drained_at",
    "sqlite_write_started_at",
)
_PIPELINE_STAGES = {
    "adapter_to_spool": ("adapter_started_at", "spool_enqueued_at"),
    "spool_queue": ("spool_enqueued_at", "spool_drained_at"),
    "spool_to_inbox": ("spool_drained_at", "inbox_enqueued_at"),
    "inbox_queue": ("inbox_enqueued_at", "inbox_drained_at"),
    "inbox_to_sqlite": ("inbox_drained_at", "sqlite_write_started_at"),
    "end_to_end": ("adapter_started_at", "sqlite_write_started_at"),
}
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


def _nearest_rank(values: Iterable[float | int]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None, "max": None}

    def percentile(numerator: int) -> float | int:
        rank = max(1, (numerator * len(ordered) + 99) // 100)
        return ordered[rank - 1]

    return {
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ordered[-1],
    }


def _delivery_timestamp(value: object) -> tuple[str, datetime | None]:
    if value is None:
        return "missing", None
    if not isinstance(value, str):
        return "invalid", None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "invalid", None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "invalid", None
    return "observed", parsed.astimezone(UTC)


def _occupancy_sample(delivery: Mapping[str, Any], name: str) -> tuple[str, tuple[int, int] | None]:
    value = delivery.get(f"{name}_occupancy", delivery.get(f"{name}_queue"))
    if value is None:
        return "missing", None
    if not isinstance(value, Mapping):
        return "invalid", None
    files = value.get("files")
    byte_count = value.get("bytes")
    if (
        not isinstance(files, int)
        or isinstance(files, bool)
        or files < 0
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        return "invalid", None
    return "observed", (files, byte_count)


def analyze_telemetry(events: Iterable[Envelope]) -> dict[str, Any]:
    """Summarize content-free delivery telemetry from persisted envelopes."""
    ordered = sorted(events, key=lambda event: (event.received_at, str(event.event_id)))
    timestamp_counts = {
        name: Counter({"observed": 0, "missing": 0, "invalid": 0}) for name in _DELIVERY_TIMESTAMPS
    }
    stage_values: dict[str, list[float]] = {name: [] for name in _PIPELINE_STAGES}
    stage_counts = {
        name: Counter({"missing": 0, "invalid": 0, "out_of_order": 0}) for name in _PIPELINE_STAGES
    }
    occupancy_values = {name: {"files": [], "bytes": []} for name in ("spool", "inbox")}
    occupancy_counts = {name: Counter({"missing": 0, "invalid": 0}) for name in occupancy_values}
    events_with_delivery = 0
    complete_traces = 0

    for event in ordered:
        delivery = event.delivery if isinstance(event.delivery, Mapping) else {}
        if delivery:
            events_with_delivery += 1
        parsed: dict[str, datetime | None] = {}
        statuses: dict[str, str] = {}
        for field in _DELIVERY_TIMESTAMPS:
            status, timestamp = _delivery_timestamp(delivery.get(field))
            statuses[field] = status
            parsed[field] = timestamp
            timestamp_counts[field][status] += 1
        full_trace = all(statuses[field] == "observed" for field in _DELIVERY_TIMESTAMPS)
        if full_trace:
            timestamps = [
                timestamp
                for field in _DELIVERY_TIMESTAMPS
                if (timestamp := parsed[field]) is not None
            ]
            full_trace = all(
                left <= right for left, right in zip(timestamps, timestamps[1:], strict=False)
            )
        if full_trace:
            complete_traces += 1

        for stage, (start_field, end_field) in _PIPELINE_STAGES.items():
            endpoint_statuses = (statuses[start_field], statuses[end_field])
            if "invalid" in endpoint_statuses:
                stage_counts[stage]["invalid"] += 1
                continue
            if "missing" in endpoint_statuses:
                stage_counts[stage]["missing"] += 1
                continue
            start = parsed[start_field]
            end = parsed[end_field]
            assert start is not None and end is not None
            seconds = (end - start).total_seconds()
            if seconds < 0:
                stage_counts[stage]["out_of_order"] += 1
                continue
            stage_values[stage].append(seconds)

        for name in occupancy_values:
            status, sample = _occupancy_sample(delivery, name)
            if sample is None:
                occupancy_counts[name][status] += 1
                continue
            occupancy_values[name]["files"].append(sample[0])
            occupancy_values[name]["bytes"].append(sample[1])

    hourly: Counter[datetime] = Counter()
    for event in ordered:
        received = event.received_at.astimezone(UTC)
        hourly[received.replace(minute=0, second=0, microsecond=0)] += 1
    peak_hour = min(
        (hour for hour, count in hourly.items() if count == max(hourly.values())),
        default=None,
    )
    first = ordered[0].received_at if ordered else None
    last = ordered[-1].received_at if ordered else None
    span = (last - first).total_seconds() if first is not None and last is not None else None
    average_per_second = len(ordered) / span if span is not None and span > 0 else None

    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "trace_coverage": {
            "events": len(ordered),
            "events_with_delivery": events_with_delivery,
            "complete_traces": complete_traces,
            "timestamp_fields": {
                field: dict(timestamp_counts[field]) for field in _DELIVERY_TIMESTAMPS
            },
        },
        "stage_durations_seconds": {
            stage: {
                "start": start,
                "end": end,
                "observed_count": len(stage_values[stage]),
                "missing_count": stage_counts[stage]["missing"],
                "invalid_count": stage_counts[stage]["invalid"],
                "out_of_order_count": stage_counts[stage]["out_of_order"],
                **_nearest_rank(stage_values[stage]),
            }
            for stage, (start, end) in _PIPELINE_STAGES.items()
        },
        "throughput": {
            "event_count": len(ordered),
            "first_received_at": first.isoformat() if first is not None else None,
            "last_received_at": last.isoformat() if last is not None else None,
            "observed_span_seconds": span,
            "average_events_per_second": average_per_second,
            "average_events_per_hour": (
                average_per_second * 3600 if average_per_second is not None else None
            ),
            "peak_hourly_rate": hourly[peak_hour] if peak_hour is not None else 0,
            "peak_hour_started_at": peak_hour.isoformat() if peak_hour is not None else None,
        },
        "occupancy": {
            name: {
                "observed_count": len(values["files"]),
                "missing_count": occupancy_counts[name]["missing"],
                "invalid_count": occupancy_counts[name]["invalid"],
                "files": _nearest_rank(values["files"]),
                "bytes": _nearest_rank(values["bytes"]),
            }
            for name, values in occupancy_values.items()
        },
    }


def _provider(event: Envelope) -> Mapping[str, Any]:
    value = event.payload.get(event.provider)
    return value if isinstance(value, dict) else {}


def captured_content(provider: Mapping[str, Any]) -> Mapping[str, Any]:
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


def exit_code(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ("exit_code", "exitCode"):
        code = value.get(key)
        if type(code) is int:
            return code
    return None


def error_exit_code(error: object) -> int | None:
    """Claude reports a failing command's exit code only as leading error text."""
    match = _EXIT_CODE_TEXT.match(error) if isinstance(error, str) else None
    return int(match.group(1)) if match is not None else None


def normalize_error_text(text: str) -> str:
    for pattern, replacement in _ERROR_NORMALIZERS:
        text = pattern.sub(replacement, text)
    return text.strip()


def tool_outcome(provider_payload: Mapping[str, Any], provider: str) -> str:
    """Judge one finished tool call; every review renders this same judgement.

    Order: Claude's own failure hook (a user interrupt is not an agent error),
    then a structured exit code or ``isError`` in a captured response, then
    Claude's ``PostToolUse``, which the provider fires only after a success.
    Codex has no failure hook and its responses carry no verdict without an
    exit code, so those stay ``unknown``: unknown is not success.
    """
    hook = provider_payload.get("hook_event_name")
    if hook == "PostToolUseFailure":
        metadata = provider_payload.get("metadata")
        interrupted = isinstance(metadata, dict) and metadata.get("is_interrupt") is True
        return "interrupt" if interrupted else "failure"
    response = captured_content(provider_payload).get("tool_response")
    code = exit_code(response)
    if code is not None:
        return "success" if code == 0 else "failure"
    if isinstance(response, dict) and response.get("isError") is True:
        return "failure"
    return "success" if hook == "PostToolUse" and provider == "claude" else "unknown"


def _failure_text(provider_payload: Mapping[str, Any]) -> tuple[int | None, str]:
    """The exit code and text of a failed call: its response, else Claude's hook error."""
    response = captured_content(provider_payload).get("tool_response")
    if response is not None:
        return exit_code(response), "\n".join(_strings(response))
    metadata = provider_payload.get("metadata")
    error = metadata.get("error") if isinstance(metadata, dict) else None
    return (error_exit_code(error), error) if isinstance(error, str) else (None, "")


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


def finding_fingerprint(rule: str, rule_version: str, evidence_ids: Iterable[str]) -> str:
    """Identify a finding by its rule and evidence.

    Findings are recomputed on every read and carry no stored identity, so a
    manual verdict needs a key that survives re-analysis.  The evidence set is
    that key: if it grows, the finding is a different observation and its
    fingerprint changes rather than silently inheriting an old verdict.
    """
    material = "\n".join((rule, rule_version, *sorted(evidence_ids)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _finding(
    rule: str, evidence_ids: list[str], explanation: str, *, attribution: str = "observed"
) -> dict[str, Any]:
    return {
        "rule": rule,
        "rule_version": RULE_VERSION,
        "evidence_ids": evidence_ids,
        "count": len(evidence_ids),
        "fingerprint": finding_fingerprint(rule, RULE_VERSION, evidence_ids),
        "attribution": attribution,
        "explanation": explanation,
    }


def _policy_finding(
    rule: str, evidence_ids: list[str], explanation: str, *, action: str
) -> dict[str, Any]:
    return {
        "rule": rule,
        "rule_version": POLICY_RULE_VERSION,
        "evidence_ids": evidence_ids,
        "count": len(evidence_ids),
        "fingerprint": finding_fingerprint(rule, POLICY_RULE_VERSION, evidence_ids),
        "attribution": "observed",
        "explanation": explanation,
        "action": action,
    }


def same_model_subagent_spawn(events: Iterable[Envelope]) -> list[dict[str, Any]]:
    """WD-014's first deterministic policy rule: a Claude subagent resolved to
    the same model as its coordinating conversation, at the moment it started
    reporting usage. One finding per spawn (``agent_id``), not per usage event.

    Structural, not statistical, so it needs no calibration and is exempt from
    the WD-013 precision gate. The rule's declared action is ``both``
    (unconditional ``log`` plus an ``intervene`` attempt); every finding here
    carries that action, matching the rule's WD-014 declaration, since this
    function's own job is only the unconditional ``log`` half -- the
    Claude-``PreToolUse``-only ``intervene`` attempt happens synchronously in
    the native adapter/daemon and is not itself recorded onto this finding. A
    subagent whose model is unavailable (not yet observed) is never compared
    -- fail-safe, not a false claim.

    Ordered by ``occurred_at`` (the true API response time), not ``received_at``
    (ingestion time): a subagent transcript is only registered at `agent.end`
    and enriched afterwards, so its usage rows can be *received* long after a
    later coordinator model switch even though they *occurred* earlier -- only
    ``occurred_at`` order compares against the model actually active at the
    time. Evidence is the matching ``agent.start`` event (the launch fact)
    plus the first matching subagent usage event; the spawning tool call's own
    prompt/tool input (on its `PreToolUse`/`tool.start` event) carries no
    `agent_id` yet at that point and cannot be linked here.
    """
    ordered = sorted(
        events, key=lambda event: (event.occurred_at or event.received_at, str(event.event_id))
    )
    coordinator_model: str | None = None
    agent_starts: dict[str, str] = {}
    matched_agents: set[str] = set()
    findings: list[dict[str, Any]] = []
    for event in ordered:
        if event.provider != "claude":
            continue
        if event.kind == "agent.start":
            if event.agent_id is not None:
                agent_starts.setdefault(event.agent_id, str(event.event_id))
            continue
        if event.kind != "usage":
            continue
        payload = event.payload.get("claude")
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model:
            continue
        if event.agent_id is None:
            coordinator_model = model
            continue
        agent_id = event.agent_id
        if agent_id in matched_agents or coordinator_model is None or model != coordinator_model:
            continue
        matched_agents.add(agent_id)
        evidence = {str(event.event_id)}
        start_id = agent_starts.get(agent_id)
        if start_id is not None:
            evidence.add(start_id)
        findings.append(
            _policy_finding(
                "same_model_subagent_spawn",
                sorted(evidence),
                f"Subagent resolved to the same model ({model}) as its coordinating conversation.",
                action="both",
            )
        )
    return findings


def control_findings(events: Iterable[Envelope]) -> list[dict[str, Any]]:
    """One finding per action the decision channel sent (WD-142).

    The daemon records every action as a ``control`` event; this projects it into
    the finding shape so a manual verdict, ``calibrate.py annotate`` and
    ``rules stats`` handle control findings like any other. Evidence is the control
    event plus the hook event it answered, so each firing has its own fingerprint
    and a replay reproduces it. A payload that lacks a rule, version or action is
    skipped: a finding without them cannot carry a verdict.
    """
    findings = []
    for event in sorted(events, key=lambda item: (item.received_at, str(item.event_id))):
        if event.kind != "control":
            continue
        recorded = _provider(event)
        rule, version, action = (recorded.get(key) for key in ("rule", "rule_version", "action"))
        answered = recorded.get("evidence_ids", [])
        if not (
            isinstance(rule, str)
            and isinstance(version, str)
            and isinstance(action, str)
            and rule
            and version
            and action
            and isinstance(answered, list)
        ):
            continue
        evidence = sorted(
            {str(event.event_id), *(item for item in answered if isinstance(item, str))}
        )
        text = recorded.get("text") or recorded.get("reason")
        findings.append(
            {
                "rule": rule,
                "rule_version": version,
                "evidence_ids": evidence,
                "count": 1,
                "fingerprint": finding_fingerprint(rule, version, evidence),
                "attribution": "observed",
                "explanation": text[:1000] if isinstance(text, str) else f"{rule}: {action}",
                "action": action,
            }
        )
    return findings


def model_family_matches(alias: str, resolved_model: str) -> bool:
    """Whether a Claude model alias (``sonnet``/``opus``/``haiku``/``fable``) names
    the same family as a fully resolved model id (e.g. ``claude-opus-5-5``).

    The Agent tool's ``tool_input.model`` is only ever an alias, visible at
    `PreToolUse` time only when the caller explicitly overrides it; a usage
    event's own observed model is always a full id. Exact equality would never
    match, so this compares at the hyphen-delimited family segment instead --
    the one comparison the `intervene` path (WD-022b) needs.
    """
    return alias.strip().lower() in resolved_model.strip().lower().split("-")


UnknownReason = Literal[
    "git_failed",
    "deadline",
    "diff_too_large",
    "too_many_untracked",
    "untracked_too_large",
    "untracked_unreadable",
]


@dataclass(frozen=True)
class CheckoutUnknown:
    """Why a checkout state could not be fingerprinted (WD-135).

    ``reason`` is a fixed, content-free code. ``detail`` is free text that may hold
    paths or Git's stderr; callers must treat it as opt-in diagnostic data.
    """

    reason: UnknownReason
    detail: str = ""


class _Unknown(Exception):
    """The checkout state could not be observed completely within the bounds."""

    def __init__(self, reason: UnknownReason, detail: str = "") -> None:
        super().__init__(reason)
        self.unknown = CheckoutUnknown(reason, detail)


def _git_output(checkout: Path, args: list[str], deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Unknown("deadline")
    try:
        # By default `git diff` silently rewrites stale stat data in the index;
        # `--no-optional-locks` does not stop that, this setting does.
        result = _run(
            ["git", "-c", "diff.autoRefreshIndex=false", "-C", str(checkout), *args],
            capture_output=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise _Unknown("deadline", f"git {args[0]} timed out") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise _Unknown("git_failed", str(error)) from error
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise _Unknown("git_failed", f"git {args[0]} exited {result.returncode}: {stderr}")
    return result.stdout


def _untracked_record(
    checkout: Path, name: bytes, budget: int, deadline: float
) -> tuple[bytes, int]:
    """One ``path, kind, content hash`` record and the file bytes it read."""
    if name.endswith(b"/"):  # a nested repository: its interior is not covered
        return name + b"\0dir\0", 0
    path = checkout / os.fsdecode(name)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):  # hash the link itself; never follow it
        target = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        return name + b"\0link\0" + target.encode(), 0
    if not stat.S_ISREG(info.st_mode):  # a FIFO or device would block on open
        return name + b"\0other\0", 0
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            size += len(chunk)
            if size > budget:
                raise _Unknown(
                    "untracked_too_large", f"{os.fsdecode(name)} exceeds the byte budget"
                )
            if time.monotonic() > deadline:
                raise _Unknown("deadline", f"hashing {os.fsdecode(name)} ran out of time")
            digest.update(chunk)
    return name + b"\0file\0" + digest.hexdigest().encode(), size


def git_diff_fingerprint(checkout: Path) -> tuple[str, int] | CheckoutUnknown:
    """Hash the checkout state as ``(digest, bytes hashed)``; failures stay unknown.

    Covers unstaged and staged tracked changes plus untracked, non-ignored files
    (path and content hash); ignored files and nested repository interiors are
    outside the signal. Read-only, and content-free: only the digest is kept.
    Any Git failure, timeout, unreadable file, or exceeded bound returns a
    ``CheckoutUnknown`` naming the reason rather than a partial or clean-looking
    value.
    """
    deadline = time.monotonic() + CHECKOUT_DEADLINE_SECONDS
    diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary"]
    try:
        unstaged = _git_output(checkout, [*diff, "--"], deadline)
        staged = _git_output(checkout, [*diff, "--cached", "--"], deadline)
        listing = _git_output(
            checkout, ["ls-files", "--others", "--exclude-standard", "-z"], deadline
        )
        if max(len(unstaged), len(staged)) > MAX_DIFF_BYTES:
            raise _Unknown(
                "diff_too_large",
                f"unstaged {len(unstaged)} and staged {len(staged)} bytes exceed {MAX_DIFF_BYTES}",
            )
        names = sorted(name for name in listing.split(b"\0") if name)
        if len(names) > MAX_UNTRACKED_FILES:
            raise _Unknown(
                "too_many_untracked", f"{len(names)} untracked files exceed {MAX_UNTRACKED_FILES}"
            )
        records = []
        untracked_bytes = 0
        for name in names:
            record, size = _untracked_record(
                checkout, name, MAX_UNTRACKED_BYTES - untracked_bytes, deadline
            )
            records.append(record)
            untracked_bytes += size
    except _Unknown as unknown:
        return unknown.unknown
    except OSError as error:  # an untracked file that vanished or is locked
        return CheckoutUnknown("untracked_unreadable", str(error))
    digest = hashlib.sha256(_CHECKOUT_FINGERPRINT_TAG)
    for section in (unstaged, staged, b"".join(_framed(record) for record in records)):
        digest.update(_framed(section))
    return digest.hexdigest(), len(unstaged) + len(staged) + untracked_bytes


def _framed(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def _is_edit(event: Envelope) -> bool:
    return event.kind == "tool.finish" and _provider(event).get("tool_name") in EDIT_TOOLS


def _change_moments(events: Iterable[Envelope], checkout: str) -> list[datetime]:
    """When an edit finished or a turn started; an event of unknown checkout counts."""
    return sorted(
        event.received_at
        for event in events
        if (event.kind == "turn.start" or _is_edit(event))
        and (event.checkout_id is None or str(event.checkout_id) == checkout)
    )


def _snapshot_time(snapshot: Mapping[str, object]) -> datetime | None:
    value = snapshot.get("observed_at")
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.utcoffset() is not None else None


def _caused_by_work(
    snapshots: tuple[Mapping[str, object], ...], moments: list[datetime]
) -> bool | None:
    """Whether an edit or turn start separates each pair of snapshots; None when undatable."""
    maybe_times = [_snapshot_time(snapshot) for snapshot in snapshots]
    times = [time for time in maybe_times if time is not None]
    if len(times) != len(maybe_times):
        return None
    return all(
        any(earlier <= moment <= later for moment in moments)
        for earlier, later in zip(times, times[1:], strict=False)
    )


def diff_oscillations(
    snapshots: Iterable[Mapping[str, object]], events: Iterable[Envelope] | None = None
) -> list[dict[str, Any]]:
    """Return A-to-B-to-A fingerprint signals, never ownership claims.

    With ``events``, a signal needs an edit or a turn start between each pair of
    snapshots: a checkout that flips with no work observed in between (a
    formatter, a branch switch) is not the agent going back and forth. Without
    events, or with a snapshot that carries no usable ``observed_at``, nothing
    is filtered: unknown is not "no work".

    Each finding also carries ``session_ids``: the sessions whose own turn
    window covered at least one of the three implicated snapshots, per an
    optional ``session_ids`` field the caller may have already resolved on
    each snapshot (see ``inspection.snapshots_for_checkouts``). An empty list
    means no session could be narrowed down, not that none is involved; more
    than one session means a concurrent editor cannot be excluded either way,
    so attribution stays "uncertain" regardless.
    """
    by_checkout: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for snapshot in snapshots:
        checkout = snapshot.get("checkout_id")
        fingerprint = snapshot.get("fingerprint")
        snapshot_id = snapshot.get("snapshot_id")
        if not all(isinstance(item, str) and item for item in (checkout, fingerprint, snapshot_id)):
            continue
        by_checkout[str(checkout)].append(snapshot)
    observed = list(events) if events is not None else None
    findings = []
    for checkout, entries in by_checkout.items():
        moments = _change_moments(observed, checkout) if observed is not None else None
        for first, second, third in zip(entries, entries[1:], entries[2:], strict=False):
            if first["fingerprint"] == third["fingerprint"] != second["fingerprint"]:
                if (
                    moments is not None
                    and _caused_by_work((first, second, third), moments) is False
                ):
                    continue
                implicated: set[str] = set()
                for triggering in (first, second, third):
                    candidates = triggering.get("session_ids")
                    if isinstance(candidates, list):
                        implicated.update(
                            session_id for session_id in candidates if isinstance(session_id, str)
                        )
                session_ids = sorted(implicated)
                finding = _finding(
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
                finding["session_ids"] = session_ids
                findings.append(finding)
    return findings


@dataclass(frozen=True, slots=True)
class _Run:
    """One call in a group of identical (tool, input, outcome) calls by one agent."""

    event_id: str
    edits: int  # finished edits seen in the session before this call
    output: str | None  # hash of the captured response; None when not captured


def _repeat_chains(runs: list[_Run]) -> list[list[_Run]]:
    """Split a group into runs of repeats that were not separated by progress.

    Two consecutive repeats are linked when no edit finished between them, or when
    they produced the same output; an edit followed by a different output is work.
    """
    chains: list[list[_Run]] = [[runs[0]]]
    for previous, current in zip(runs, runs[1:], strict=False):
        same_output = current.output is not None and current.output == previous.output
        if current.edits == previous.edits or same_output:
            chains[-1].append(current)
        else:
            chains.append([current])
    return chains


def _repeat_explanation(chain: list[_Run]) -> str:
    quiet = len({run.edits for run in chain}) == 1
    same = chain[0].output is not None and len({run.output for run in chain}) == 1
    if quiet and same:
        detail = "same output, no edits between"
    elif quiet:
        detail = "no edits between"
    elif same:
        detail = "same output despite edits between"
    else:
        detail = "each repeat followed no edit or reproduced the previous output"
    return f"Observed the same tool input and outcome {len(chain)} times in a row ({detail})."


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
    edits_seen: Counter[tuple[str, str | None]] = Counter()
    repeated: dict[tuple[tuple[str, str | None, str | None], str], list[_Run]] = defaultdict(list)
    errors: dict[str, list[str]] = defaultdict(list)
    failing_tests: dict[str, list[str]] = defaultdict(list)
    usage: dict[str, int] = defaultdict(int)
    usage_records = 0
    usage_unavailable = False
    claude_responses: list[dict[str, int | None]] = []
    claude_requests: set[str] = set()
    codex_responses: set[tuple[str | None, str]] = set()
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
        if event.kind == "usage" and event.provider == "claude":
            # Per-response counters, not cumulative: count each requestId once.
            request_id = provider.get("request_id") or event.native_event_id
            if isinstance(request_id, str):
                if request_id in claude_requests:
                    continue
                claude_requests.add(request_id)
            record = provider.get("usage")
            counters = claude_response_usage(
                record.get("response") if isinstance(record, dict) else None
            )
            if counters is None:
                usage_unavailable = True
            else:
                claude_responses.append(counters)
            continue
        if event.kind == "usage":
            # A response stored twice under different event ids (pre-WD-128
            # Codex reader) counts once.
            if event.native_event_id is not None:
                response = (event.session_id, event.native_event_id)
                if response in codex_responses:
                    continue
                codex_responses.add(response)
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
        captured = captured_content(provider)
        command = captured.get("tool_input")
        response = captured.get("tool_response")
        outcome = tool_outcome(provider, event.provider)
        outcomes[outcome] += 1
        session = (event.provider, event.session_id)
        edits_before = edits_seen[session]
        if _is_edit(event):
            edits_seen[session] += 1
        if response is not None:
            output_available += 1
            output_bytes += len(_canonical(response).encode("utf-8"))
        if command is None and response is None:
            # Nothing captured to compare; grouping such calls by tool alone
            # would call every call of one tool a repeat.
            continue
        agent = (event.provider, event.session_id, event.agent_id)
        signature = _fingerprint(
            {"tool": provider.get("tool_name"), "input": command, "outcome": outcome}
        )
        repeated[(agent, signature)].append(
            _Run(
                str(event.event_id),
                edits_before,
                _fingerprint(response) if response is not None else None,
            )
        )
        if outcome != "failure":
            continue
        code, text = _failure_text(provider)
        if code is None and not text:
            continue
        error_signature = _fingerprint({"code": code, "error": normalize_error_text(text)})
        errors[error_signature].append(str(event.event_id))
        tests = _test_failures(command, text)
        if tests:
            test_signature = _fingerprint({"input": command, "failures": tests})
            failing_tests[test_signature].append(str(event.event_id))
    if claude_responses:
        usage_records += len(claude_responses)
        for name in USAGE_COLUMNS:
            observed = [
                value for counters in claude_responses if (value := counters[name]) is not None
            ]
            # A counter missing from any response is unknown, not a partial sum.
            if len(observed) == len(claude_responses):
                usage[name] += sum(observed)
            elif name in _CLAUDE_REQUIRED_USAGE:
                usage_unavailable = True

    findings: list[dict[str, Any]] = []
    for runs in repeated.values():
        for chain in _repeat_chains(runs):
            if len(chain) >= REPETITION_THRESHOLD:
                findings.append(
                    _finding(
                        "repeated_tool_outcome",
                        [run.event_id for run in chain],
                        _repeat_explanation(chain),
                    )
                )
    for evidence_ids in errors.values():
        if len(evidence_ids) >= REPETITION_THRESHOLD:
            findings.append(
                _finding(
                    "identical_error",
                    evidence_ids,
                    f"Observed the same failing tool result {len(evidence_ids)} times "
                    "(paths and numbers normalised).",
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
    target_sessions = set(sessions)
    for oscillation in diff_oscillations(snapshots, ordered):
        implicated = oscillation["session_ids"]
        # An empty set means attribution could not be narrowed (no turn boundary
        # observed for the checkout); keep the old inclusive behavior rather than
        # manufacturing a false negative. A non-empty set that misses this batch's
        # sessions means the oscillation belongs to other sessions on the same
        # checkout, so it is dropped here, not just deduplicated.
        if implicated and not (set(implicated) & target_sessions):
            continue
        findings.append(oscillation)
    findings.extend(same_model_subagent_spawn(ordered))
    findings.extend(control_findings(ordered))
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
            "tool_outcomes": {
                name: outcomes[name] for name in ("failure", "interrupt", "success", "unknown")
            },
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
