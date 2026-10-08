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

None of the six v2 findings on the WD-122 cohort has a recorded verdict. The table
below is an assistant's contextual read of the retained timelines, **not a verdict**:
the verdicts stay with the user, and precision stays unmeasured until they are
recorded.

| v2 fingerprint | Rule | Provider | Repeats | Read |
| --- | --- | --- | --- | --- |
| `ff7365b5aa06` | `repeated_tool_outcome` | Codex | 5 | Waiting on a sub-agent (`wait_agent`) for 304 s with two edits between; same output. Looks like a false positive. |
| `16b783772132` | `repeated_tool_outcome` | Codex | 4 | Polling for an exit-code file of a background run for 133 s, same output, no edits. Looks like a false positive. |
| `56ef48f43d18` | `repeated_tool_outcome` | Codex | 3 | Polling a process for 127 s, three different outputs, no edits. Looks like a false positive. |
| `e68d67580762` | `repeated_tool_outcome` | Claude | 3 | Re-reading one file across 8,626 s and 60 edits, identical content. Looks like a false positive. |
| `dfe304b00890` | `identical_error` | Claude | 4 | The same lint command failed four times in 45 s on a locked directory. Looks like a true positive. |
| `9ffcf21826f8` | `identical_error` | Claude | 5 | The same test launch failed five times in 36 s on an interpreter spawn error. Looks like a true positive. |

On that read the provisional precision is 0 of 4 for `repeated_tool_outcome` (the 60%
target is not met) and 2 of 2 for `identical_error`. These are counts of six
findings, one reviewer's reading, and not a gate input.

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

## Limits

- Polling and waiting calls (`wait_agent`, background-run status checks) still trip
  `repeated_tool_outcome`. Excluding them by tool name would be fitted to this
  cohort; the follow-up (WD-161) waits for user verdicts.
- Codex outcomes remain unknown, so Codex repeats are keyed on the outcome `unknown`.
- The oscillation gate recognises only edit tools and turn starts. A checkout changed
  through the shell (`git checkout`/`stash`, a formatter, `Set-Content`) reads as "no
  work", so some of the 7 oscillations dropped on WD-122 may have been the agent's own
  doing. Unknown is not zero; the rule follows the ticket and the gap is recorded here.
- The cohort is the owner's own sessions on one machine; events added after the
  freeze and retention-stripped content can move a finding.
- All four rules stay observe-only; no rule clears any rung of the M3 ladder.
