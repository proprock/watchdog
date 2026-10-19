# WD-145 design review

Status: specification review resolved by the user, 2026-10-18. Implementation
and acceptance remain open. This is a design decision, not deployment
authorization or live acceptance evidence.
The [architecture decision](../architecture.md#wd-145-semantic-judge-review-2026-10-18)
defines the currently accepted planned contract.

## Accepted diagnostic triggering

After explicit project opt-in and selection of diagnostic triggers, analysis may
run without prior calibration of the originating signal. The user selected this
over waiting for calibration/promotion before any automatic analysis. Budget,
timeout, isolation and kill switches still apply. Sending an intervention remains
separately gated; analysis approval is not action/promotion approval.

## Accepted delivery default

The user selected log/status only by default, with context enabled separately.
Context requires an approved enabled delivery rule and a confirmed capability.
An unsupported event retains log/status and never silently substitutes block.
The alternative of automatic context delivery upon judge opt-in was not selected.

## Accepted configurability requirement

The user explicitly requested configuration for all similar behavioral options,
so they can tailor the process to their preferences. This includes triggers and
thresholds, backend/model/effort, timeout, daily budget, cooldown, delivery,
pending-result lifetime and turn policy, and pause behavior. Provide validated
defaults, project overrides and inspectable effective settings. Preferences may
not bypass TENETS invariants, approval, confirmed capabilities or hard limits.
Implement config readers and examples together, not unsupported sample keys now.

## Accepted pause/disable default

The user selected cancelling the active call on pause or judge disablement.
Terminate only owned provider CLI processes/descendants, record `cancelled`, and
clean up owned processes before scratch/authentication material. Already-started
calls count toward the budget. Local cancellation does not guarantee remote
provider cancellation. Finish-within-timeout remains configurable, but may retain
only log/status; both modes suppress new calls and delivery while paused/disabled.

## Accepted pending-result scope

The user explicitly confirmed that configured result validity may carry an
undelivered result beyond its source turn and, after the source session ends,
into a new session of the same project. Never transfer it to a different project.
Configuration takes priority over an implicit turn/session-end cutoff.

Preserve source identity/evidence and record the target separately. Before any
delivery, recheck configured validity/scope, source evidence relevance to the
target context, rule approval/enablement, capabilities, project/global opt-ins
and kill switches. Unknown relevance or stale evidence means log/status only.
Structured analysis and suggested labels remain distinct from human verdicts and
actual rule-authorized actions. Track delivery attempts across restart without
claiming that a sent response proves provider effect.

## Accepted budget accounting

The user selected durable reservation before launch, charging started or uncertain
analysis attempts. A slot is returned only when the analysis CLI demonstrably
did not start. Failure after launch and cancellation do not refund it. The
alternative of charging all reservations, even proven nonlaunch, remains a
configurable stricter mode rather than the default.

Implementation must persist budget, trigger/evidence deduplication and cooldown
state. Crash-unknown reservations stay charged. Restarts, duplicate triggers and
day rollover must not recreate the same call or bypass the current launch-day
cap. The daily boundary is a documented configurable preference; reload must not
erase charged history. These are correctness contracts for the selected budget
policy, not authorization for actual model calls.

## Accepted initial settings

The user selected a 120-second timeout, accepting the other proposed starting
values. This resolves the old 60-versus-120-second discrepancy.

| Setting | Default |
|---|---|
| Judge enabled | false |
| Delivery | log/status only; context enabled separately |
| Timeout | 120 seconds |
| Per-project daily calls | 5 |
| Per-session cooldown | 10 minutes |
| Result validity from completion | 30 minutes |
| Result scope | same project, including a new session after source end |
| Pause/disable | cancel the owned analysis call |
| Accounting | started/unknown attempts charged; proven nonlaunch refundable |

All comparable preferences are configurable with validated defaults and project
overrides. Config does not waive approval, capabilities, kill switches, hard
budget enforcement or fail-open invariants. Implementation and separately
authorized live acceptance remain open, including WD-166 for Codex analysis and
the applicable WD-151/WD-153 delivery cells. WD-147's promotion review is separate.

No model call, user-local deployment, actual rule approval or TENETS change was
authorized by these design selections.
