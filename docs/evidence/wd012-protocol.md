# WD-012 labelling protocol v1

The rules a reviewer follows while labelling the frozen cohort. It exists so the
calibration report can name its labelling procedure instead of an unwritten
agreement, and so a second pass, or a second reviewer, produces comparable
labels. It does not authorize any harness change, and no step here invokes a
provider.

Dataset: [wd012-sample.json](wd012-sample.json), frozen 2026-09-09 over 48
considered sessions. It holds 24 settled sessions - 4 that produced a
session-scoped finding and 20 drawn under seed 12 from the 20 that produced none
- plus 8 checkout-scoped `diff_oscillation` findings listed once. The silent
sessions are in the cohort so false negatives stay countable; skipping them
destroys the measurement.

Tool: `uv run python scripts/calibrate.py annotate --project PROJECT`. Answers
go through the control inbox to the running core; the review process never
writes to the store itself. Quitting is safe: the next run resumes at the first
session that carries no label.

## What each label means

`progress_state` answers what happened to the work, and is the label the rules
are measured against.

| State | Applies when |
| --- | --- |
| `progress` | The session kept moving toward the task. Failed tool calls that were resolved are still progress. |
| `slow` | The work advanced, but turns were spent on repetition or rework a reviewer can point to. |
| `stuck` | The session stopped advancing and repeated itself. |
| `externally_blocked` | The session waited on something outside the agent's control: a human decision, an unavailable host, a vendor outage. |

`task_outcome` answers what became of the task: `success`, `partial`, `failed`,
`abandoned`, or `unknown`. It is the outcome of the task, never of the tools. A
turn that completed and a tool that exited zero are not evidence of success;
`unknown` is a legitimate answer and is not to be rounded to `success`.

`task_type` is free text, so keep a short fixed vocabulary for the whole pass
(for example `feature`, `debugging`, `docs`, `research`, `validation`) and reuse
the exact strings. A vocabulary invented per session makes the distribution in
the report unusable.

`reviewer_note` is required whenever `progress_state` is `stuck` or `slow` on a
session with no finding: the report prints that note as the justification of a
false negative. One sentence naming the evidence, not an impression.

A finding verdict is `true_positive` when the finding describes something that
really went wrong, `false_positive` when it fired on healthy work, and
`uncertain` when the retained evidence does not settle it. `uncertain` is kept
separate from both sides in the report and is not a way of avoiding a decision;
it is the honest answer when the capture is insufficient.

## Reviewing one card

1. Read the header and the `content` line. Where a reply or a tool result was not
   stored, the card cannot support a judgement about what the agent said: treat
   that as missing evidence, not as silence.
2. Read the timeline with `l`, widening to `l 50` or `l all` for a long session.
   Look for the same command repeating, long offsets between rows, failures that
   were never resolved, and how the last turn ended.
3. Use `x` only when the card and the timeline do not settle whether the person
   got what they asked for. The vendor transcript is the full record.
4. Record `p`, then `o`, then `t`, then `r`. The order does not matter; each
   keystroke resends the whole label.
5. Where the card lists findings, record a verdict per numbered finding after
   reading its evidence rows. Judge the frozen list. If the drift line reports
   that a live re-analysis now differs, ignore the difference: the report reads
   the frozen sample, and chasing a new finding would label a cohort nobody
   defined.
6. Move on with `n`.

## Across the pass

Review the checkout-scoped findings once with `c`, not once per session. A
`diff_oscillation` is evidence about a checkout's diff history; the current
schema cannot attribute it to the session that caused it (WD-118), so it is
excluded from per-session precision.

Do not label from memory of the session. Where the retained evidence is
insufficient, say so in the note and choose `unknown` or `uncertain`; an
optimistic guess is indistinguishable from data in the report and destroys its
value as an evidence gate for WD-013.

## Boundaries

Labelling changes no harness behaviour, installs no hook, and calls no provider.
Content read during the review stays on screen: the evidence files in this
directory keep identifiers, counts, and manual labels, never prompts, assistant
output, or absolute paths.

## Update: WD-118 attribution (post-dates this pass)

This protocol and the frozen sample it labelled predate WD-118. WD-118 added
session attribution for `diff_oscillation` (see [storage.md](../storage.md#diff_oscillation-session-attribution-wd-118)):
a checkout-wide oscillation is now reported only to the session(s) whose own
turn window covered its capture, instead of to every session that ever
touched the checkout. It did not change the evidence identifiers a finding
carries, so the manual verdicts already recorded here against this frozen
sample remain valid and do not need re-review. It also did not move
`diff_oscillation` out of `CHECKOUT_SCOPED_RULES`: the checkout-scoped review
step above still applies until a fresh `scripts/calibrate.py sample`/`report`
pass against the live store, and a separate decision, re-scores it.
