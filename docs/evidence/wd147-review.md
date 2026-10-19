# WD-147 design review

Status: specification review resolved by the user, 2026-10-18. Implementation,
dependencies and acceptance remain open. This is a design decision, not runtime
changes, rule promotion or live acceptance evidence.

## Accepted threshold enforcement

The user selected advisory enforcement by default: stats reports threshold
eligibility and risks, but a human may explicitly confirm manual promotion with
insufficient sample size or precision. Strict enforcement remains configurable.
The alternative of strict-by-default enforcement was not selected. Thresholds
are configurable in either mode, and there is no automatic promotion.

Record manual promotion/demotion, evidence scope, observed eligibility and any
explicit advisory override. A human override does not turn insufficient evidence
into measured precision. Code approval, confirmed capabilities, opt-in and kill
switches remain mandatory. Promotion neither approves code nor enables a disabled
candidate.

The [architecture review](../architecture.md#wd-147-calibration-and-promotion-review-2026-10-18)
holds the accepted planned contract. Reuse WD-144's independent format and
human-confirmed classification; never reuse old-body precision as evidence for
a new body or pool providers into a qualifying sample.

## Accepted separate policy storage

The user selected a policy record separate from code, for the specific rule body
and provider, preserving history. Promotion/demotion alone does not rewrite
approved bytes, so it does not require fresh code approval. The alternative of
rewriting the action in the rule file, making it stale after every policy change,
was not selected. A grant cannot authorize actions or payloads unsupported by
the approved rule.

## Accepted changed-body handling

The user selected offering restoration of the former level at fresh code approval,
through a separate explicit confirmation. Record the restoration as manual
promotion for the new body and warn that its reviewed sample is empty/insufficient.
The default advisory mode permits a human-confirmed override; strict mode still
requires the new body's qualifying evidence.

The alternative of requiring a later standalone promote without offering
restoration during reapproval was not selected. Code approval alone does not
restore the former level, and neither code approval nor restoration enables a
disabled candidate. Without a confirmed new grant, permission is at most log.
Changed code never runs before fresh hash approval and never inherits old-body
precision. Keep former grants and verdicts as historical records.

## Accepted sample unit

The user selected unique human-reviewed findings as the default: count an evidence
fingerprint once within provider/version/body scope and display distinct session
counts separately. The session-based alternative remains configurable, rather
than the default. Do not treat uncertain or unconfirmed model suggestions as TP,
FP or reviewed cases. Show `TP / (TP + FP)`, denominator, review coverage and cohort;
precision without reviewed cases is unavailable. Reannotation changes the verdict
for one case with an audit history, not the sample size.

## Accepted precision-drop reaction

The user selected warnings and a demotion recommendation, retaining the current
level until manual action. There is no automatic promotion or demotion by default.
The alternative of automatic demotion is a configurable opt-in; it may only lower
the level and must record the change. It cannot restore a level automatically.
Strict promotion mode does not silently convert this default into automatic
demotion. Revoked approval, stale code and kill switches always take effect.

## Accepted eligibility defaults and deterministic exemptions

The user selected retaining configurable deterministic exemptions. For
human-confirmed deterministic rules, log/context have no calibration minimum;
ask/rewrite/deny/block require at least 10 confirmed TP and 0 FP for eligibility.
Approval and manual promotion remain mandatory. The alternative of applying the
same criteria to both types by default was not selected.

The selected statistical starting recommendations are log without a minimum,
context >=60% on 10 reviewed cases, ask >=80% on 20, and rewrite/deny/block >=90%
on 30. These remain configurable recommendations in advisory mode and enforced
criteria for new promotion requests in strict mode. They do not prove general
accuracy or grant unapproved/unsupported actions.

## Accepted prefill confirmation

The user selected per-case confirmation by default: show the case, evidence and
suggested TP/FP/uncertain; the human confirms or corrects it. Suggestions remain
suggested until that explicit action, preserve provenance and never overwrite a
human verdict. Confirmed uncertain cases remain outside TP/FP and the qualifying
reviewed minimum. Mismatched provider/version/body/fingerprint cannot label the
current case.

Batch confirmation remains configurable rather than the default. It requires
explicit case selection and confirmation, recording membership and provenance;
it never silently accepts all suggestions. Fresh model analysis must be explicitly
requested through the selected available backend; this review launches no call.

## Implementation and acceptance still open

Keep WD-142/WD-145/WD-146 dependencies, WD-166's selected-backend availability,
WD-167's Codex cohort and WD-162's recomputation contract distinct from design
approval. Validate configuration and effective settings; exercise provider-free
contracts for labels, unique counts, scope isolation, action criteria, modes,
separate grants, explicit restoration and invariant enforcement. Measure annotation
of 20 sessions before/after prefill and report real denominators and coverage.
Do not equate suggestions, manual promotion or delivered text with proven provider
effect or task success.

No actual rule is approved, enabled or promoted by this review; no model call or
TENETS change is authorized.
