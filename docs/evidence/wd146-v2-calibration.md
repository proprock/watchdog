# WD-012 calibration report

Generated 2026-10-20T18:57:18.926965+00:00 from `wd122-v2-sample.json` (sha256 `0bad9e3eab1416778e3aeb997ae3e04bd32a5dac063b4c8c0dccdc107a19b7cd`).

## Dataset

- Sessions: 46
- Rule version: wd-010.v2
- Storage schema: v7
- Selection: {"all_eligible": true, "considered": 81, "eligible": 46, "min_events": 20, "provider": null, "seed": 12, "settle_hours": 2.0, "since": null, "skipped": {"below_min_events": 33, "not_settled": 2}, "strata": {"with_finding": 14, "without_finding_available": 32, "without_finding_drawn": 32}, "target": 40, "until": null}

## Session states

- progress: 16
- slow: 6
- unlabeled: 24

## Session-scoped rule precision

| rule | observed | reviewed | TP | FP | uncertain | precision |
| --- | --- | --- | --- | --- | --- | --- |
| identical_error | 2 | 2 | 1 | 1 | 0 | 50.00% |
| repeated_test_failure | 0 | 0 | 0 | 0 | 0 | null |
| repeated_tool_outcome | 4 | 4 | 2 | 0 | 2 | 100.00% |

## False negatives

5 (sessions judged slow or stuck with no session-scoped finding).

## Hook overhead

- In-hook p50/p95: 0.7 / 11.8 ms
- End-to-end p50/p95: 1156.1 / 102075287.0 ms
- Measured on 22593 of 39747 events. Transcript-sourced events carry no delivery trace, so the denominator is smaller than the event count. Null is not zero.

## Recommendations

- identical_error: not recommended — 50.00% precision (n=2) is below the 90% gate.
- repeated_test_failure: not recommended (n=0) — no observation in this dataset.
- repeated_tool_outcome: not recommended — 4 of 4 findings reviewed, with 2 uncertain; reviewed precision 100.00% (n=2) cannot clear the gate while verdicts are missing or uncertain.
- No rule cleared the 90% gate in this dataset; M3 selects no rule for delivery yet.

## Limitations

- Rules with no observation in this dataset: repeated_test_failure.
- Checkout-scoped findings are excluded from per-session precision.
- One operator labelled their own sessions; the judgement is not independent.
- The session count is a calibration target, not proof of representativeness.
