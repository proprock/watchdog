"""WD-122 offline comparison report contract."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def report_tool():
    path = Path(__file__).parents[1] / "scripts" / "wd122_report.py"
    spec = importlib.util.spec_from_file_location("wd122_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample() -> dict:
    return {
        "format_version": "wd-012.sample.v1",
        "sessions": [
            {
                "provider": "codex",
                "session_id": "noisy",
                "findings": [
                    {
                        "rule": "repeated_tool_outcome",
                        "fingerprint": "m2-finding",
                        "evidence_ids": ["event-1", "event-2"],
                    },
                    {
                        "rule": "same_model_subagent_spawn",
                        "fingerprint": "policy-finding",
                        "evidence_ids": ["event-3"],
                    },
                ],
            },
            {"provider": "claude", "session_id": "quiet", "findings": []},
            {"provider": "codex", "session_id": "unreviewed", "findings": []},
        ],
        "checkout_findings": [
            {
                "rule": "diff_oscillation",
                "fingerprint": "checkout-finding",
                "evidence_ids": ["event-4", "event-5"],
                "session_ids": ["noisy", "quiet", "noisy"],
            }
        ],
    }


def baseline() -> dict:
    return {
        "format_version": "wd-012.calibration.v3",
        "session_scoped_rules": {
            "repeated_tool_outcome": {"observed": 8, "true_positive": 2, "false_positive": 6},
            "identical_error": {"observed": 0, "true_positive": 0, "false_positive": 0},
        },
        "checkout_scoped_rules": {"rules": {"diff_oscillation": {"observed": 8}}},
    }


def review(sample_bytes: bytes) -> dict:
    return {
        "format_version": "wd-122.review.v1",
        "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "findings": [
            {
                "fingerprint": "m2-finding",
                "verdict": "true_positive",
                "reason": "The command was retried after the same failure.",
                "evidence_ids": ["event-1", "event-2"],
            }
        ],
        "sessions": [
            {
                "provider": "claude",
                "session_id": "quiet",
                "progress_state": "slow",
                "reason": "Repeated setup work delayed progress.",
                "evidence_ids": ["event-5"],
            }
        ],
        "availability": {"events": 5, "gaps": 1},
    }


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    sample_path = tmp_path / "sample.json"
    baseline_path = tmp_path / "baseline.json"
    sample_bytes = json.dumps(sample(), sort_keys=True).encode()
    sample_path.write_bytes(sample_bytes)
    baseline_path.write_text(json.dumps(baseline()), encoding="utf-8")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review(sample_bytes)), encoding="utf-8")
    return baseline_path, sample_path, review_path


def test_counts_denominators_and_checkout_scope(report_tool, tmp_path):
    baseline_path, sample_path, review_path = write_inputs(tmp_path)

    report = report_tool.build_report(baseline_path, sample_path, review_path)

    repeated = report["session_scoped_rules"]["repeated_tool_outcome"]
    assert repeated["before"]["observed"] == 8
    assert repeated["after"] == {
        "observed": 1,
        "true_positive": 1,
        "false_positive": 0,
        "uncertain": 0,
        "unreviewed": 0,
        "precision": 1.0,
        "precision_denominator": 1,
    }
    assert report["session_scoped_rules"]["identical_error"]["after"]["precision"] is None
    assert report["policy_findings"] == {"same_model_subagent_spawn": {"observed": 1}}
    checkout = report["checkout_scoped_rules"]["diff_oscillation"]
    assert checkout["observed"] == 1
    assert checkout["session_id_distribution"] == {"noisy": 2, "quiet": 1}
    assert "precision" not in checkout
    assert report["false_negative_limits"]["reviewed_slow_or_stuck_without_finding"] == [
        {"provider": "claude", "session_id": "quiet", "progress_state": "slow"}
    ]
    assert report["false_negative_limits"]["unreviewed_silent_sessions"] == [
        {"provider": "codex", "session_id": "unreviewed"}
    ]
    assert report["guidance"]["gate"] == "closed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.__setitem__("sample_sha256", "wrong"),
        lambda document: document["findings"][0].__setitem__("evidence_ids", ["event-1"]),
        lambda document: document["findings"][0].__setitem__("verdict", "maybe"),
        lambda document: document["sessions"][0].__setitem__("session_id", "missing"),
    ],
)
def test_rejects_bad_digest_or_frozen_evidence(report_tool, tmp_path, mutate):
    baseline_path, sample_path, review_path = write_inputs(tmp_path)
    document = json.loads(review_path.read_text(encoding="utf-8"))
    mutate(document)
    review_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        report_tool.build_report(baseline_path, sample_path, review_path)


def test_zero_observations_and_markdown_are_explicit(report_tool, tmp_path):
    baseline_path, sample_path, review_path = write_inputs(tmp_path)
    report = report_tool.build_report(baseline_path, sample_path, review_path)

    assert report["session_scoped_rules"]["identical_error"]["after"]["precision_denominator"] == 0
    assert report["session_scoped_rules"]["identical_error"]["after"]["unreviewed"] == 0
    markdown = report_tool.render_markdown(report)
    assert "Before / after M2 findings" in markdown
    assert "Guidance remains closed" in markdown
    assert "The command was retried" in markdown
