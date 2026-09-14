# WD-012 calibration report

Generated 2026-09-14T21:42:44.718844+00:00 from `wd012-sample.json` (sha256 `8ee766b4c7eb1a78f2eac9ff853c7d15d0c962ffbf06c92c8ff0b13a21f77d73`).

## Dataset

- Sessions: 24
- Rule version: wd-010.v1
- Storage schema: v7
- Selection: {"considered": 48, "eligible": 24, "min_events": 20, "provider": null, "seed": 12, "settle_hours": 2.0, "since": null, "skipped": {"below_min_events": 20, "not_settled": 4}, "strata": {"with_finding": 4, "without_finding_available": 20, "without_finding_drawn": 20}, "target": 40, "until": null}

## Session states

- progress: 16
- slow: 6
- unlabeled: 2

## Session-scoped rule precision

| rule | observed | reviewed | TP | FP | uncertain | precision |
| --- | --- | --- | --- | --- | --- | --- |
| identical_error | 0 | 0 | 0 | 0 | 0 | null |
| repeated_test_failure | 0 | 0 | 0 | 0 | 0 | null |
| repeated_tool_outcome | 8 | 8 | 2 | 6 | 0 | 25.00% |

## False negatives

4 (sessions judged slow or stuck with no session-scoped finding).

## Hook overhead

- In-hook p50/p95: 5.1 / 13.7 ms
- End-to-end p50/p95: 810.9 / 2106.2 ms
- Measured on 4915 of 10839 events. Transcript-sourced events carry no delivery trace, so the denominator is smaller than the event count. Null is not zero.

## Recommendations

- identical_error: not recommended (n=0) — no observation in this dataset.
- repeated_test_failure: not recommended (n=0) — no observation in this dataset.
- repeated_tool_outcome: not recommended — 25.00% precision (n=8) is below the 90% gate.
- No rule cleared the 90% gate in this dataset; M3 selects no rule for delivery yet.

## Limitations

- Rules with no observation in this dataset: identical_error, repeated_test_failure.
- Checkout-scoped findings are excluded from per-session precision.
- One operator labelled their own sessions; the judgement is not independent.
- The session count is a calibration target, not proof of representativeness.
