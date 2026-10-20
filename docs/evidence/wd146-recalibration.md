# WD-146 recalibration: `wd-010.v1` to `wd-010.v2`

Measured on 2026-10-08 with `scripts/calibrate.py report --rule-version wd-010.v2`
against the live store, read-only. The two frozen cohorts are the
[WD-012 sample](wd012-sample.json) (24 sessions) and the
[WD-122 sample](wd122-sample.json) (46 sessions, which contains the first 24). The
v2 findings are recomputed from the stored events of those sessions; the samples
keep their v1 findings and are not rewritten. No threshold changed
(`REPETITION_THRESHOLD` stays 3).

## What changed in v2

- `tool_outcome` is one function for every reader (`analyze`, `inspection.finish_status`,
  the daemon's session state, `calibrate.py`). Claude's `PostToolUseFailure` is a
  failure (an `is_interrupt` one is `interrupt`, not an agent error), a structured
  `exit_code`/`isError` wins next, and Claude's `PostToolUse` is `success`. Codex has no
  failure hook and no exit code in most responses, so it stays `unknown`.
- `repeated_tool_outcome` groups per agent and needs three repeats in a chain where
  each repeat followed no edit (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`,
  `apply_patch`) or reproduced the previous output. Its explanation now says which.
- `identical_error` groups on the exit code plus the error text with paths, quoted
  values, hex ids and numbers normalised; for Claude the text is the failure hook's
  `error`, because a failed call carries no `tool_response`.
- `diff_oscillation` needs an edit or a turn start between each pair of snapshots.
  Without events, or without a dated snapshot, nothing is filtered.
- The report's `tool_outcomes` gains an `interrupt` key (`REPORT_SCHEMA_VERSION` 3).

## Outcome classification (WD-122 cohort, 46 sessions)

| Provider | v1 | v2 |
| --- | --- | --- |
| Claude | 100% unknown | 2,764 success, 70 failure, 0 unknown |
| Codex | 1,690 unknown | 1,690 unknown |

Every Claude `tool.finish` carries `PostToolUse` or `PostToolUseFailure`, so none is
left unknown. Codex is unchanged: its hook responses carry no exit code or error
flag in this cohort, which is a provider limit, not something to guess around.

## Findings, v1 against v2

A v1 finding *survives* when a v2 finding of the same rule shares an event with it.
v1 verdicts are shown for context; they never transfer, because a v2 evidence set may
differ. "Dropped TP/FP" uses the stored or reviewed v1 verdict of each dropped finding.

| Cohort | Rule | v1 | v1 TP / FP / uncertain | Dropped | Dropped TP / FP / uncertain | v2 | v2 without a v1 counterpart |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WD-012 | `repeated_tool_outcome` | 8 | 2 / 6 / 0 | 7 | 1 / 6 / 0 | 1 | 0 |
| WD-012 | `identical_error` | 0 | - | 0 | - | 0 | 0 |
| WD-012 | `repeated_test_failure` | 0 | - | 0 | - | 0 | 0 |
| WD-012 | `diff_oscillation` | 8 | unreviewed | 5 | - | 3 | 0 |
| WD-122 | `repeated_tool_outcome` | 19 | 2 / 15 / 2 | 15 | 1 / 12 / 2 | 4 | 0 |
| WD-122 | `identical_error` | 0 | - | 0 | - | 2 | 2 |
| WD-122 | `repeated_test_failure` | 0 | - | 0 | - | 0 | 0 |
| WD-122 | `diff_oscillation` | 14 | unreviewed | 7 | - | 6 | 0 |

- On WD-122, `repeated_tool_outcome` drops 12 of 15 false positives (all 6 on WD-012)
  and 2 uncertain findings, but also one of its two true positives: three `list_agents`
  polls with edits between them and different output. That is the cost of requiring
  "no edits or the same output"; it was not tuned away (see WD-161).
- `identical_error` now has observations (2 on WD-122, both Claude). Under v1 it could
  not fire for Claude at all.
- Each session's events are cut at its recorded `last_received_at` and diff snapshots at
  the sample's `generated_at`, so the table measures the rule change and not data that
  arrived later. An uncut first run gave the same counts. No cohort session was missing
  from the store and none changed its event count.

## Precision of the v2 findings

Refreshed on 2026-10-20 by scoring the unchanged frozen
`wd122-v2-sample.json` against the local store. Two human verdicts had already been
recorded on 2026-10-08 (UTC): `dfe304b00890` is a true positive and `9ffcf21826f8`
is a false positive, both without a reviewer note. The latter differs from the
assistant's earlier assessment. The recorded verdict takes precedence; its reason
must not be inferred from that assessment.

The [exported verdicts](wd146-v2-review.json) preserve the full fingerprints,
evidence IDs and review timestamps. On 2026-10-20 the user reviewed the four
remaining findings: `ff7365b5aa06` and `16b783772132` are `uncertain`, while
`56ef48f43d18` and `e68d67580762` are true positives. Those verdicts were recorded
through the same daemon annotation channel used by `calibrate.py annotate` and
confirmed by reading the store. No additional reason was supplied, and none is
inferred. The refreshed
[report](wd146-v2-calibration.md) ([JSON](wd146-v2-calibration.json)) scores the
same frozen sample without recomputing findings or transferring v1 verdicts.

| Rule | Observed | Reviewed | TP | FP | Uncertain | Precision |
| --- | --- | --- | --- | --- | --- | --- |
| `repeated_tool_outcome` | 4 | 4 | 2 | 0 | 2 | 100% (2/2 decided) |
| `identical_error` | 2 | 2 | 1 | 1 | 0 | 50% (1/2) |

Precision is TP / (TP + FP); `uncertain` is reviewed but excluded from that
denominator. All six findings now have a human verdict, including two explicitly
uncertain cases. The user verdicts, rather than the assistant's earlier reading,
are the calibration input:

| v2 fingerprint | Recorded verdict | Reviewed on (UTC) |
| --- | --- | --- |
| `ff7365b5aa06` | uncertain | 2026-10-20 |
| `16b783772132` | uncertain | 2026-10-20 |
| `56ef48f43d18` | true positive | 2026-10-20 |
| `e68d67580762` | true positive | 2026-10-20 |
| `dfe304b00890` | true positive | 2026-10-08 |
| `9ffcf21826f8` | false positive | 2026-10-08 |

| Rule | WD-122 v1 precision | WD-122 v2 precision |
| --- | --- | --- |
| `repeated_tool_outcome` | 11.76% (2 TP / 15 FP; 2 uncertain) | 100% (2 TP / 0 FP; 2 uncertain) |
| `identical_error` | unmeasured (no observations) | 50% (1 TP / 1 FP) |

The versions have different evidence sets and decided denominators (17 versus 2
for repeats), so this is a disclosed small-sample comparison, not proof of
generalisation. No threshold was tuned and no rule clears an M3 delivery rung:
the repeat sample has two uncertain cases and only two decided observations;
`identical_error` is below the first delivery rung and also has only two
observations. WD-146 is complete; broader calibration and polling decisions stay
with WD-167 and WD-161.

### Historical assistant assessment (not verdicts)

The table below preserves the assistant's original contextual read, **not
verdicts**. It is superseded by the recorded verdicts above.

| v2 fingerprint | Rule | Provider | Repeats | Read |
| --- | --- | --- | --- | --- |
| `ff7365b5aa06` | `repeated_tool_outcome` | Codex | 5 | Waiting on a sub-agent (`wait_agent`) for 304 s with two edits between; same output. Looks like a false positive. |
| `16b783772132` | `repeated_tool_outcome` | Codex | 4 | Polling for an exit-code file of a background run for 133 s, same output, no edits. Looks like a false positive. |
| `56ef48f43d18` | `repeated_tool_outcome` | Codex | 3 | Polling a process for 127 s, three different outputs, no edits. Looks like a false positive. |
| `e68d67580762` | `repeated_tool_outcome` | Claude | 3 | Re-reading one file across 8,626 s and 60 edits, identical content. Looks like a false positive. |
| `dfe304b00890` | `identical_error` | Claude | 4 | The same lint command failed four times in 45 s on a locked directory. Looks like a true positive. |
| `9ffcf21826f8` | `identical_error` | Claude | 5 | The same test launch failed five times in 36 s on an interpreter spawn error. Looks like a true positive. |

On that original read the provisional precision was 0 of 4 for
`repeated_tool_outcome` and 2 of 2 for `identical_error`. Those assistant estimates
are not a gate input; the recorded `identical_error` precision is 1 of 2.

## Reproduce and record verdicts

The recomputed cohorts and reports are committed beside the v1 samples:
[wd012-v2-sample.json](wd012-v2-sample.json),
[wd122-v2-sample.json](wd122-v2-sample.json),
[wd012-v2-calibration.md](wd012-v2-calibration.md) and
[wd122-v2-calibration.md](wd122-v2-calibration.md) (with `.json`/`.csv` twins).

```
uv run python scripts/calibrate.py report --config CONFIG --data DATA --runtime RUNTIME \
  --project PROJECT --sample docs/evidence/wd122-sample.json --rule-version wd-010.v2 \
  --review docs/evidence/wd122-review.json
```

Verdicts for the v2 findings are recorded with `calibrate.py annotate --sample
docs/evidence/wd122-v2-sample.json` (same `--config/--data/--runtime/--project` as the
installed hook); `report --sample docs/evidence/wd122-v2-sample.json` then scores them.

The final 2026-10-20 score was produced without `--rule-version`, retaining the
frozen v2 findings:

```
uv run python scripts/calibrate.py report --config CONFIG --data DATA --runtime RUNTIME \
  --project PROJECT --sample docs/evidence/wd122-v2-sample.json \
  --output docs/evidence/wd146-v2-calibration.json
```

Verification matched the frozen sample's SHA-256, all six fingerprints and exact
evidence ID lists to the exported verdicts, and checked report TP/FP/uncertain
counts and precision. `git diff --check` passed. This completion changes only
calibration records and documentation; no runtime code or thresholds changed.

## Limits

- Polling and waiting calls (`wait_agent`, background-run status checks) still trip
  `repeated_tool_outcome`. Excluding them by tool name would be fitted to this
  cohort. The final user review judged two such findings true positive and two
  uncertain, so WD-161 cannot assume all four are false positives.
- Codex outcomes remain unknown, so Codex repeats are keyed on the outcome `unknown`.
- The oscillation gate recognises only edit tools and turn starts. A checkout changed
  through the shell (`git checkout`/`stash`, a formatter, `Set-Content`) reads as "no
  work", so some of the 7 oscillations dropped on WD-122 may have been the agent's own
  doing. Unknown is not zero; the rule follows the ticket and the gap is recorded here.
- The cohort is the owner's own sessions on one machine; events added after the
  freeze and retention-stripped content can move a finding.
- All four rules stay observe-only; no rule clears any rung of the M3 ladder.
