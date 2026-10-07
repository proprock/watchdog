"""Build an offline WD-122 comparison report from frozen JSON evidence."""

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REVIEW_FORMAT = "wd-122.review.v1"
REPORT_FORMAT = "wd-122.report.v1"
VERDICTS = frozenset({"true_positive", "false_positive", "uncertain"})
PROGRESS_STATES = frozenset({"progress", "slow", "stuck", "externally_blocked", "unknown"})
POLICY_RULES = frozenset({"same_model_subagent_spawn"})
CHECKOUT_RULES = frozenset({"diff_oscillation"})


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def require_list(document: Mapping[str, Any], name: str, label: str) -> list[dict[str, Any]]:
    value = document.get(name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label}.{name} must be a list of objects")
    return value


def require_text(item: Mapping[str, Any], name: str, label: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{name} must be non-empty text")
    return value


def evidence_ids(item: Mapping[str, Any], label: str) -> list[str]:
    value = item.get("evidence_ids")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(entry, str) and entry for entry in value)
    ):
        raise ValueError(f"{label}.evidence_ids must be a non-empty list of text IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{label}.evidence_ids contains duplicate IDs")
    return value


def sample_inventory(
    sample: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str]], set[str]]:
    findings: dict[str, dict[str, Any]] = {}
    sessions: set[tuple[str, str]] = set()
    all_evidence: set[str] = set()
    for record in require_list(sample, "sessions", "sample"):
        provider = require_text(record, "provider", "sample.sessions[]")
        session_id = require_text(record, "session_id", "sample.sessions[]")
        key = (provider, session_id)
        if key in sessions:
            raise ValueError("sample contains duplicate provider/session IDs")
        sessions.add(key)
        for finding in require_list(record, "findings", "sample.sessions[]"):
            fingerprint = require_text(finding, "fingerprint", "sample.findings[]")
            if fingerprint in findings:
                raise ValueError("sample contains duplicate finding fingerprints")
            ids = evidence_ids(finding, "sample.findings[]")
            findings[fingerprint] = finding
            all_evidence.update(ids)
    for finding in require_list(sample, "checkout_findings", "sample"):
        fingerprint = require_text(finding, "fingerprint", "sample.checkout_findings[]")
        if fingerprint in findings:
            raise ValueError("sample contains duplicate finding fingerprints")
        ids = evidence_ids(finding, "sample.checkout_findings[]")
        findings[fingerprint] = finding
        all_evidence.update(ids)
    return findings, sessions, all_evidence


def validate_review(
    review: Mapping[str, Any], sample_path: Path, sample: Mapping[str, Any]
) -> None:
    if review.get("format_version") != REVIEW_FORMAT:
        raise ValueError(f"review.format_version must be {REVIEW_FORMAT}")
    digest = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    if review.get("sample_sha256") != digest:
        raise ValueError("review.sample_sha256 does not match the frozen sample bytes")

    frozen, sessions, _ = sample_inventory(sample)
    seen_fingerprints: set[str] = set()
    for finding in require_list(review, "findings", "review"):
        fingerprint = require_text(finding, "fingerprint", "review.findings[]")
        verdict = require_text(finding, "verdict", "review.findings[]")
        require_text(finding, "reason", "review.findings[]")
        ids = evidence_ids(finding, "review.findings[]")
        if fingerprint in seen_fingerprints:
            raise ValueError("review contains duplicate finding fingerprints")
        seen_fingerprints.add(fingerprint)
        frozen_finding = frozen.get(fingerprint)
        if frozen_finding is None:
            raise ValueError("reviewed finding is absent from the frozen sample")
        if frozen_finding.get("rule") in POLICY_RULES or "action" in frozen_finding:
            raise ValueError("policy findings are observations and cannot receive M2 verdicts")
        if frozen_finding.get("rule") in CHECKOUT_RULES:
            raise ValueError("checkout-scoped findings cannot receive session precision verdicts")
        if ids != frozen_finding.get("evidence_ids"):
            raise ValueError("reviewed finding evidence_ids do not exactly match the frozen sample")
        if verdict not in VERDICTS:
            raise ValueError("reviewed finding has an unknown verdict")

    seen_sessions: set[tuple[str, str]] = set()
    for session in require_list(review, "sessions", "review"):
        provider = require_text(session, "provider", "review.sessions[]")
        session_id = require_text(session, "session_id", "review.sessions[]")
        progress = require_text(session, "progress_state", "review.sessions[]")
        require_text(session, "reason", "review.sessions[]")
        evidence_ids(session, "review.sessions[]")
        key = (provider, session_id)
        if key in seen_sessions:
            raise ValueError("review contains duplicate provider/session IDs")
        seen_sessions.add(key)
        if key not in sessions:
            raise ValueError("reviewed session is absent from the frozen sample")
        if progress not in PROGRESS_STATES:
            raise ValueError("reviewed session has an unknown progress_state")

    availability = review.get("availability")
    if not isinstance(availability, dict) or not availability:
        raise ValueError("review.availability must be a non-empty object of numeric counts")
    for name, value in availability.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            raise ValueError("review.availability must contain numeric counts")
        if value < 0:
            raise ValueError("review.availability counts cannot be negative")


def after_stats(observed: int, verdicts: Counter[str]) -> dict[str, Any]:
    true_positive = verdicts["true_positive"]
    false_positive = verdicts["false_positive"]
    uncertain = verdicts["uncertain"]
    denominator = true_positive + false_positive
    reviewed = denominator + uncertain
    return {
        "observed": observed,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "uncertain": uncertain,
        "unreviewed": observed - reviewed,
        "precision": round(true_positive / denominator, 4) if denominator else None,
        "precision_denominator": denominator,
    }


def baseline_stats(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: value.get(field)
        for field in (
            "observed",
            "reviewed",
            "true_positive",
            "false_positive",
            "uncertain",
            "precision",
            "precision_denominator",
        )
        if field in value
    }


def build_report(baseline_path: Path, sample_path: Path, review_path: Path) -> dict[str, Any]:
    baseline = load_object(baseline_path, "baseline report")
    sample = load_object(sample_path, "sample")
    review = load_object(review_path, "review")
    validate_review(review, sample_path, sample)

    records = require_list(sample, "sessions", "sample")
    frozen, _, _ = sample_inventory(sample)
    observed: Counter[str] = Counter()
    policy_observed: Counter[str] = Counter()
    session_m2_finding: dict[tuple[str, str], bool] = {}
    provider_mix: Counter[str] = Counter()
    for record in records:
        provider = record["provider"]
        session_id = record["session_id"]
        provider_mix[provider] += 1
        session_m2_finding[(provider, session_id)] = False
        for finding in require_list(record, "findings", "sample.sessions[]"):
            rule = require_text(finding, "rule", "sample.findings[]")
            # A finding with an `action` (the policy rule, or a control action
            # since WD-142) is an observation, never an M2 shadow finding.
            if rule in POLICY_RULES or "action" in finding:
                policy_observed[rule] += 1
            elif rule not in CHECKOUT_RULES:
                observed[rule] += 1
                session_m2_finding[(provider, session_id)] = True

    verdicts: dict[str, Counter[str]] = {}
    examples: list[dict[str, Any]] = []
    for item in require_list(review, "findings", "review"):
        rule = str(frozen[item["fingerprint"]]["rule"])
        verdicts.setdefault(rule, Counter())[item["verdict"]] += 1
        examples.append(
            {
                "kind": "finding",
                "fingerprint": item["fingerprint"],
                "evidence_ids": item["evidence_ids"],
                "reason": item["reason"],
            }
        )

    session_reviews: dict[tuple[str, str], dict[str, Any]] = {}
    states: Counter[str] = Counter()
    for item in require_list(review, "sessions", "review"):
        key = (item["provider"], item["session_id"])
        session_reviews[key] = item
        states[item["progress_state"]] += 1
        examples.append(
            {
                "kind": "session",
                "provider": item["provider"],
                "session_id": item["session_id"],
                "evidence_ids": item["evidence_ids"],
                "reason": item["reason"],
            }
        )

    baseline_rules = baseline.get("session_scoped_rules", {})
    if not isinstance(baseline_rules, dict):
        raise ValueError("baseline report session_scoped_rules must be an object")
    rules = sorted((set(baseline_rules) | set(observed)) - POLICY_RULES - CHECKOUT_RULES)
    session_stats = {
        rule: {
            "before": baseline_stats(baseline_rules.get(rule, {})),
            "after": after_stats(observed[rule], verdicts.get(rule, Counter())),
        }
        for rule in rules
    }

    checkout_observed: Counter[str] = Counter()
    distributions: dict[str, Counter[str]] = {}
    for finding in require_list(sample, "checkout_findings", "sample"):
        rule = require_text(finding, "rule", "sample.checkout_findings[]")
        if rule != "diff_oscillation":
            continue
        checkout_observed[rule] += 1
        distribution = distributions.setdefault(rule, Counter())
        session_ids = finding.get("session_ids")
        if not isinstance(session_ids, list) or not all(
            isinstance(item, str) and item for item in session_ids
        ):
            raise ValueError("checkout finding session_ids must be a list of text IDs")
        distribution.update(session_ids)

    previous_checkout = baseline.get("checkout_scoped_rules", {})
    previous_checkout_rules = (
        previous_checkout.get("rules", {}) if isinstance(previous_checkout, dict) else {}
    )
    checkout_report = {
        rule: {
            "before": baseline_stats(previous_checkout_rules.get(rule, {})),
            "observed": checkout_observed[rule],
            "session_id_distribution": dict(sorted(distributions.get(rule, Counter()).items())),
            "note": "Checkout-scoped evidence is excluded from session precision.",
        }
        for rule in sorted(set(previous_checkout_rules) | set(checkout_observed))
    }

    missed = [
        {
            "provider": provider,
            "session_id": session_id,
            "progress_state": review_item["progress_state"],
        }
        for (provider, session_id), review_item in sorted(session_reviews.items())
        if review_item["progress_state"] in {"slow", "stuck"}
        and not session_m2_finding[(provider, session_id)]
    ]
    silent_unreviewed = [
        {"provider": provider, "session_id": session_id}
        for provider, session_id in sorted(session_m2_finding)
        if not session_m2_finding[(provider, session_id)]
        and (provider, session_id) not in session_reviews
    ]
    return {
        "format_version": REPORT_FORMAT,
        "dataset": {
            "baseline_report": baseline_path.name,
            "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
            "baseline_rule_version": baseline.get("dataset", {}).get("rule_version"),
            "sample": sample_path.name,
            "sample_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
            "review": review_path.name,
            "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "rule_version": sample.get("rule_version"),
            "selection": sample.get("selection"),
            "sessions": len(records),
        },
        "provider_mix": dict(sorted(provider_mix.items())),
        "availability": review["availability"],
        "review": {
            "findings_reviewed": len(review["findings"]),
            "sessions_reviewed": len(review["sessions"]),
            "session_progress_states": dict(sorted(states.items())),
        },
        "session_scoped_rules": session_stats,
        "policy_findings": {
            rule: {"observed": count} for rule, count in sorted(policy_observed.items())
        },
        "checkout_scoped_rules": checkout_report,
        "false_negative_limits": {
            "reviewed_slow_or_stuck_without_finding": missed,
            "unreviewed_silent_sessions": silent_unreviewed,
        },
        "examples": examples,
        "guidance": {
            "gate": "closed",
            "reason": "WD-122 is an offline partial review; no rule is promoted by this report.",
        },
        "limitations": [
            "Precision uses reviewed M2 findings only; unreviewed and uncertain findings remain "
            "unresolved.",
            "Recall is limited to reviewed slow or stuck sessions without an M2 finding; "
            "silent sessions without review remain unmeasured.",
            "Checkout diff oscillation is reported separately and never contributes to "
            "session-scoped precision.",
        ],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    dataset = report["dataset"]
    lines = [
        "# WD-122 offline comparison report",
        "",
        f"Frozen sample: `{dataset['sample']}` (sha256 `{dataset['sample_sha256']}`).",
        "",
        "## Dataset",
        "",
        f"- Baseline report: `{dataset['baseline_report']}`",
        f"- Baseline rule version: `{dataset['baseline_rule_version']}`",
        f"- Expanded rule version: `{dataset['rule_version']}`",
        f"- Sample sessions: {dataset['sessions']}",
        f"- Selection: {json.dumps(dataset['selection'], sort_keys=True)}",
        f"- Provider mix: {json.dumps(report['provider_mix'], sort_keys=True)}",
        f"- Availability: {json.dumps(report['availability'], sort_keys=True)}",
        "",
        "## Before / after M2 findings",
        "",
        "| rule | baseline observed | baseline TP / FP / n | baseline precision | "
        "expanded observed | expanded TP / FP / uncertain / n | unreviewed | "
        "expanded precision | change |",
        "| --- | ---: | --- | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for rule, entry in report["session_scoped_rules"].items():
        before, after = entry["before"], entry["after"]
        precision = "null" if after["precision"] is None else f"{after['precision']:.2%}"
        old_precision = before.get("precision")
        baseline_precision = "null" if old_precision is None else f"{old_precision:.2%}"
        change = (
            "unmeasured"
            if old_precision is None or after["precision"] is None
            else f"{(after['precision'] - old_precision) * 100:+.2f} pp"
        )
        lines.append(
            f"| {rule} | {before.get('observed', 0)} | "
            f"{before.get('true_positive', 0)} / {before.get('false_positive', 0)} / "
            f"{before.get('precision_denominator', 0)} | {baseline_precision} | "
            f"{after['observed']} | {after['true_positive']} / {after['false_positive']} / "
            f"{after['uncertain']} / {after['precision_denominator']} | "
            f"{after['unreviewed']} | {precision} | {change} |"
        )
    lines += ["", "## Policy observations", ""]
    for rule, stats in report["policy_findings"].items():
        lines.append(f"- {rule}: observed {stats['observed']} time(s); excluded from M2 precision.")
    lines += ["", "## Checkout diff oscillation", ""]
    for rule, stats in report["checkout_scoped_rules"].items():
        lines.append(
            f"- {rule}: observed {stats['observed']} time(s); implicated session IDs "
            f"{json.dumps(stats['session_id_distribution'], sort_keys=True)}. {stats['note']}"
        )
    false_negative = report["false_negative_limits"]
    lines += [
        "",
        "## False-negative limits",
        "",
        "- Reviewed slow/stuck sessions without an M2 finding: "
        f"{len(false_negative['reviewed_slow_or_stuck_without_finding'])}",
        f"- Silent sessions left unreviewed: {len(false_negative['unreviewed_silent_sessions'])}",
        "- Reviewed progress states: "
        + json.dumps(report["review"]["session_progress_states"], sort_keys=True),
        "",
        "## Review examples",
        "",
    ]
    for example in report["examples"]:
        identity = example.get("fingerprint") or f"{example['provider']}/{example['session_id']}"
        lines.append(
            f"- `{identity}` evidence {', '.join(example['evidence_ids'])}: {example['reason']}"
        )
    lines += [
        "",
        "## Guidance",
        "",
        "Guidance remains closed unconditionally. " + report["guidance"]["reason"],
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-report", required=True, type=Path)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.baseline_report, args.sample, args.review)
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"report": str(args.output), "markdown": str(args.output.with_suffix(".md"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
