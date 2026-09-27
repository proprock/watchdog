# WD-122 finding-quality error analysis

The frozen [WD-012 sample](wd012-sample.json) and its [manual verdicts](wd012-calibration.json)
remain the baseline. The [WD-122 sample](wd122-sample.json) contains all 46 settled
sessions with at least 20 events at capture time: all 24 frozen WD-012 identities
and 22 additional cases (11 Codex, 11 Claude). Its SHA-256 is recorded in
[the review record](wd122-review.json), which carries each finding's complete
evidence-ID set and a separate manual verdict. The review contains no captured
commands, prompts, or output.

## Six baseline false positives

| Finding fingerprint | Evidence anchors (first, last) | Cause | Proposed remedy |
| --- | --- | --- | --- |
| `44351b3994c8` | `ae035faf-1aea-4217-aae4-8304c9beb67f`, `071a2681-f64a-4892-906d-82bb6a479648` | The same pytest command progressed from 88 to 89 passing tests across edits and turns. Repetition was productive. | Compare test-result progression and intervening edits before treating a repeated test command as stalled; current capture does not prove a general rule. |
| `7b4e10f73f49` | `bc2a36bf-d3e3-45d5-a9fc-446329d4dfc2`, `fcbaa61c-0d03-4def-a114-357fe91619d8` | A static index-status result was queried during routine preflight across turns. | Keep status polling distinct from task failure; an exclusion for this one tool would overfit the cohort. |
| `a65a59e63c51` | `df2d126e-de78-4db4-ab66-fe731976916b`, `e3bedc00-49e4-4153-a27b-84916207bcdf` | Environment and daemon probes had different outputs, intervening edits, and three observation gaps. | Improve result normalization and capture coverage before asserting that the outcomes matched. |
| `da7cd8c7d8b4` | `9ba96841-5a58-4a18-afcd-60f2d4e51948`, `16989cf1-8e49-4d7a-9369-ea9acd4d754a` | Claude reread a file while edits and writes advanced the work. | Account for intervening work and turn context; repeated reads alone are not a stall. |
| `4b9dc1a5b44c` | `06d4b4dd-3348-4aab-bf73-f5d33ba1548b`, `af6a8a48-f4a5-4d20-ae37-72179212b635` | Ruff progressed from an access error to lint errors and then a clean result. | Preserve structured failure and success distinctions, including output progression. |
| `ce92f3c4eba8` | `730b8290-9402-4a94-89cd-8b5c0bbf9977`, `ac85307a-73a3-40d0-a48f-93b855a7fdfd` | Build attempts alternated between access errors and build output around edits. | Compare normalized outcomes and intervening work before calling build retries unproductive. |

## Four baseline false negatives

These sessions were manually marked `slow` in WD-012 and had no session-scoped
finding. The anchors below identify each retained timeline; specific event IDs
and the verdict provenance are in the review record.

| Session | Evidence anchors (first, last) | Missed pattern and limit |
| --- | --- | --- |
| `01a07e28-900f-7b23-bc55-2f2571ce1bb7` | `7dd8ad68-2331-40bc-ad0b-667d7feaba5d`, `b4e0f14e-faf8-5d87-9b0b-6f7acf15edab` | Broad exploration used distinct graph queries and snippets; exact-input repetition cannot see it. Two late gaps limit confidence. |
| `01a07f91-bce6-7fe0-abe5-8ed2470049a4` | `2b37afd7-34d2-42f1-afe4-851cfdec4d9b`, `5d120066-5ca3-523e-9ba2-0778d365d702` | Test, venv, and sandbox retries used changing commands; test-command comparability and environment cause need separate evidence. |
| `01a07faa-fa6e-7fa1-9e26-63e718d30732` | `045e74c3-0a4b-4ebd-aad9-1f0662423a72`, `e1d1355d-b4d1-483a-9d57-d41b29b13754` | An early turn ended after one Git call, followed by a long delay and different commands. Turn/time boundaries matter more than exact tool repetition. |
| `01a08236-2423-7570-909d-9967f4913195` | `df4d5fac-2a98-4f5d-912c-de6cb500b98d`, `c4c8845c-c7a9-557e-8979-1ccf077c20bc` | Similar research queries had different literal inputs, so the exact matcher missed semantic repetition. |

## Decision and expanded review

The two baseline true positives (`8184a3581c5e` and `0f0d672b6f18`) also have
`unknown` normalized tool outcomes. Requiring a known outcome would remove
both; raising the threshold to four would remove one. No general-purpose
suppression is justified by these observations, so `wd-010.v1` remains the rule
version. The 11 additional repeated-tool findings were reviewed separately:
9 false positives, 2 uncertain, and no defensible true positive. Their
fingerprints, reasons, and complete evidence IDs are in the review record.
These are one reviewer's contextual judgments, not an independent adjudication.

The additional 12 sessions with no finding include 10 with likely progress,
one whose slow-progress status remains unresolved, and one without a captured
initial request. Three more sessions have only deterministic policy findings
and remain unreviewed for statistical recall. These judgments do not establish
recall: missing task context, 70 unavailable tool responses, 346 observation
gaps, and unknown normalized outcomes for all 4,454 captured tool responses
remain. The provider outcome-availability field is unknown for all 4,524 tool
finishes, including the 70 without a captured response.
Every one of the 15,042 retained events has unknown provider version and
surface provenance. The 14 checkout-scoped `diff_oscillation` findings remain
unreviewed; WD-118 narrows the implicated sessions, but concurrent work still
prevents a unique author attribution.

The evidence supports a safer calibration gate and broader review, but no
versioned finding-rule change. All statistical rules remain observe-only.

## Reproduction

The report is deterministic from the committed JSON evidence:

```console
uv run python scripts/wd122_report.py --baseline-report docs/evidence/wd012-calibration.json --sample docs/evidence/wd122-sample.json --review docs/evidence/wd122-review.json --output docs/evidence/wd122-report.json
```

The sample was captured with `scripts/calibrate.py sample --all-eligible
--min-events 20 --settle-hours 2 --output docs/evidence/wd122-sample.json`
against the local live project. Repeating that capture later may select more
sessions; the committed sample and its SHA-256 freeze the comparison here.
Availability counts in the review were aggregated over those 46 session IDs
from the read-only `events` table: `availability.tool_outcome`, captured
`tool_response`, `provider_version`, `surface`, and `observation.gap` events.
