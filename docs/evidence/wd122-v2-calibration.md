# WD-012 calibration report

Generated 2026-10-08T05:46:24.314409+00:00 from `wd122-sample.json` (sha256 `507831ac1b7eb6b85a61117942af1e6ea2fdc39cab54de3d67b8081803433c6a`).

## Dataset

- Sessions: 46
- Rule version: wd-010.v2
- Storage schema: v7
- Selection: {"all_eligible": true, "considered": 81, "eligible": 46, "min_events": 20, "provider": null, "seed": 12, "settle_hours": 2.0, "since": null, "skipped": {"below_min_events": 33, "not_settled": 2}, "strata": {"with_finding": 14, "without_finding_available": 32, "without_finding_drawn": 32}, "target": 40, "until": null}

## Session states

- progress: 16
- slow: 6
- unlabeled: 24

## Version comparison (wd-010.v1 to wd-010.v2)

A v1 finding survives when a v2 finding of the same rule shares an event with it. v1 verdicts are shown for context and never transfer: every v2 finding is unreviewed until someone reads it.

| rule | v1_observed | v1_true_positive | v1_false_positive | v1_uncertain | v1_unreviewed | v1_dropped | dropped_true_positive | dropped_false_positive | v2_observed | v2_new |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| repeated_tool_outcome | 19 | 2 | 15 | 2 | 0 | 15 | 1 | 12 | 4 | 0 |
| identical_error | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 |
| repeated_test_failure | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| diff_oscillation | 14 | 0 | 0 | 0 | 14 | 7 | 0 | 0 | 6 | 0 |

Excluded (no stored events): 0; event count changed since the sample: 0.

## Session-scoped rule precision

| rule | observed | reviewed | TP | FP | uncertain | precision |
| --- | --- | --- | --- | --- | --- | --- |
| identical_error | 2 | 0 | 0 | 0 | 0 | null |
| repeated_test_failure | 0 | 0 | 0 | 0 | 0 | null |
| repeated_tool_outcome | 4 | 0 | 0 | 0 | 0 | null |

## False negatives

5 (sessions judged slow or stuck with no session-scoped finding).

## Hook overhead

- In-hook p50/p95: 0.6 / 13.9 ms
- End-to-end p50/p95: 883.9 / 2592.7 ms
- Measured on 13780 of 24167 events. Transcript-sourced events carry no delivery trace, so the denominator is smaller than the event count. Null is not zero.

## Recommendations

- identical_error: not recommended — observed 2 time(s) but never reviewed; precision is unmeasured.
- repeated_test_failure: not recommended (n=0) — no observation in this dataset.
- repeated_tool_outcome: not recommended — observed 4 time(s) but never reviewed; precision is unmeasured.
- No rule cleared the 90% gate in this dataset; M3 selects no rule for delivery yet.

## Limitations

- Findings were recomputed from the live store under wd-010.v2; sessions with missing events are excluded and listed, and events added or purged since the sample was frozen can move a finding.
- Rules with no observation in this dataset: repeated_test_failure.
- Checkout-scoped findings are excluded from per-session precision.
- One operator labelled their own sessions; the judgement is not independent.
- The session count is a calibration target, not proof of representativeness.
