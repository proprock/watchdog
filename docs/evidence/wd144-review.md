# WD-144 design review

Status: approval/replay workflow and classification selected by the user,
2026-10-18. WD-144's specification review is resolved; implementation remains open.
This record is a design decision, not implementation or live acceptance evidence.

## Accepted candidate lifecycle

The user selected one code approval: `approve` -> replay -> separate `enable`.
Unchanged code needs no second approval before enablement. The alternative of
separate replay-only and live-execution approval scopes was not selected.

The [architecture decision](../architecture.md#wd-144-candidate-approval-and-replay-decision-2026-10-18)
defines the planned contract. Generation and syntax validation execute no proposed
Python code. Approval binds to exact reviewed bytes before replay. Candidates stay
inactive after approval and replay; activation is an explicit separate operation.
Replay produces hypothetical results on saved evidence and never delivers actions
to live sessions. Changed code invalidates approval. WD-143's cooperative-thread
execution limitations apply to Python replay.

This resolves the disputed pre-approval replay requirement without changing
TENETS.md or creating a one-shot execution exception. It does not approve any
actual code body, authorize a live model call, or waive precision/promotion gates.

## Accepted format and classification

The user selected model-proposed deterministic/statistical classification,
explicitly confirmed by the human at `rules approve`. Implementation format
(TOML/Python) is stored separately; neither dimension implies the other. The
confirmed classification is recorded with approval. The alternative of treating
all model proposals as statistical until a separate manual reclassification was
not selected.

Editing candidate metadata cannot silently change a previously human-confirmed
classification. Reclassification requires explicit human review and a recorded
update; it still does not automatically promote the rule.

Deterministic rules enforce specified policy; statistical rules infer a problem
from behavioral evidence. A deterministic label alone grants neither activation
nor stronger intervention. Promotion is governed by the subsequently accepted
[WD-147 decision](../architecture.md#wd-147-calibration-and-promotion-review-2026-10-18).
This decision does not authorize a rule or model call.
