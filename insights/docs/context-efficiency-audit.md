# Context efficiency audit

## Decision

Prioritize bounded output and narrower discovery before adopting earlier compaction.
Keep native compaction as the default. The corpus demonstrates large avoidable search
outputs, but does not prove that earlier summarization saves subscription allowance
without degrading task completion. API-equivalent prices are a separate comparison,
not a conversion to subscription quota.

## Corpus and reproducibility

The local 2026-09-05 audit rebuilt 723 sessions from 756 available source files
(562 Claude files including subagent streams, 194 Codex files). Parsing reported
zero malformed-line warnings. The previous database had 1,799 sessions; 1,084 of
its recorded source paths were no longer available. The new corpus is therefore
not a complete reproduction of that older database. The current working session
is included and continues to grow; use the generated JSON snapshot for exact counts.

The corpus starts on 2026-03-31 and ends on 2026-09-05. Provider/model/client-version
and background-session cohorts are included in the generated audit JSON. Default
dashboard cohorts exclude background sessions and require measurable calls.

Run with the normal WDI configuration pointing at a freshly rebuilt database:

```powershell
uv run wdi import --full
uv run python -m wdi.context_audit --db PATH_TO_REBUILT_DATABASE --out .cache/context-audit.json
uv run wdi serve
```

The schema remains rebuildable: remove an incompatible local database before
importing. Source session files are never edited. Generated audit JSON contains
local diagnostic previews and must not be committed or sent to an external advisor.
The dashboard advisor receives aggregates only.

The final full replay took approximately 160 seconds on this machine; a fresh
import with media sanitization took approximately 173 seconds while checks were
running. These are local wall-clock observations, not portable benchmarks.
Unchanged files are skipped. Changed streams are replayed in full so a response
completed in a later import reconciles identically to a fresh import. This trades
additional work on growing files for correctness; byte-offset resume optimization
must preserve response identity and tool-result correlation before reintroduction.

## Findings and review method

Reviewed the 20 largest tool-output additions and 20 largest error records per
provider, including the next three tool calls for each error. These are ranked
diagnostic samples; overlapping error windows are not independent experiments.
Raw provider IDs and excerpts remain in the local audit artifact.

* Claude growth: 13 of the first 20 candidates were reads of the large project-a
  TODO document, including bounded reads with long lines. Other examples were
  large C++ files, hardware documentation, a customer reply and echoed plan text.
  Large size alone does not establish irrelevance: hardware evidence and task
  requirements can be essential. Prefer section/symbol-level reads and archive
  completed TODO details outside the active queue when maintaining those projects.
* Codex growth: the initial ranking was dominated by image transport payloads.
  Their base64 bytes were incorrectly counted as visible text. After correction,
  the largest text additions include browser/tool bootstrap documentation, broad
  graph queries, file reads, search results and deferred output. Tool bootstrap
  documentation is not automatically removable from subsequent requests.
* Codex errors: the reviewed set contains 14 search/log timeouts, other overly
  broad searches/listings, two test failures, and a file-comparison invocation.
  Several recursive searches include generated binaries, symbol databases or
  virtual environments. Following calls often switch successfully to bounded
  file ranges or graph queries. One pair repeats a binary-heavy search before
  switching approach. Narrowing discovery is directly actionable.
* Claude errors: 19 of the 20 candidates concern builds, live tests or composite
  build/run commands; one is a search returning exit code 2. Continuations show
  edits, baseline comparisons and environment diagnosis, not simply unchanged
  retries. Preserve the actual failure evidence; reduce surrounding build output
  and check environment prerequisites. Do not classify all failed tests as waste.

There are only four observed Claude compaction boundaries in the default cohort.
Codex has 103, but trigger attribution remains unknown. Neither cohort supports
a causal manual-versus-auto comparison or a historical comparison of prompt variants.
Exact provider counters, observations of history, size estimates and hypothetical
savings are deliberately separate.

## Measurement corrections

* Codex input already includes cached input. Context size is input, and uncached
  input is input minus cache read. Claude input, cache read and cache creation are
  separate components. Reasoning is not added again to output.
* Claude response fragments share provider message identity. Component maxima
  reconcile cumulative observations; original values and the method remain in
  event metadata. Codex cumulative usage notifications are reconciled within
  their recorded turn/generation coordinates. Empty/reset usage is not a call.
  Missing identities remain explicitly unidentified; equality of token sizes
  alone never merges unrelated calls.
* Claude turns count actual user messages, excluding tool results, metadata and
  compaction summaries. Main streams and subagent streams remain independent.
  Recursive child-session call counts do not add provider subagent summary tokens.
* Identical instruction-slot replacements do not count as newly added content.
  Changed targets are compared within an event/category, so a tool input does
  not make its first output a repeated read. Exact repetition still does not prove waste.
* Overlap is a bounded normalized-line-hash estimate, using at most 256 distinct
  nontrivial lines per block. It is not a token estimate or a semantic staleness detector.
* Media/ciphertext transport is opaque. Visible token estimates use approximately
  four characters per token and exclude transport data. Provider-internal requests,
  hidden instructions, silent clearing and opaque reasoning remain unobservable.

## Using the new controls

Rank by calls, user turns, total or uncached input, cache reads/writes, output,
peak context, visible bytes, repeats or API-equivalent cost. Choose whether
aggregates/advice apply to all filtered sessions, Top N, or selected checkboxes.
The table is the display list; the count above it identifies the analyzed population.

The advisor estimate is computed from the actual aggregate prompt before any
LLM invocation. Output budget is an estimate, not an enforced CLI generation cap.
Unknown tariffs stay unknown. Model aliases without configured matching prices
are not silently mapped to a possibly different current model.

The scenario calculator compares continuation, early compact, preventing output
before ingestion and avoiding retry calls. Inputs are explicit assumptions:
remaining calls, cache hit rate, removed fraction, preserved prefix, summary size,
reacquisition, avoidable output/calls and continuation output. Claude cache policy
prices misses as cache writes; Codex prices them as uncached input. Sensitivity
varies cache hit by ten percentage points. Break-even is reported only when the
reduction can amortize compaction and cache warmup. Context/output growth and
task-quality effects beyond those inputs are not predicted.

## Native experiments

```powershell
uv run python -m wdi.context_profiles claude --threshold native
uv run python -m wdi.context_profiles claude --threshold 70 --record .cache/run-70.json --launch
uv run python -m wdi.context_profiles claude --threshold 85 --launch
uv run python -m wdi.context_profiles codex --threshold 70 --window 200000 --launch
uv run python -m wdi.context_profiles claude --record .cache/run-70.json --session-id SESSION_ID --outcome success
```

Without `--launch`, commands only describe the profile. `--record` saves a local
versioned JSON manifest; outcomes are `success`, `state-loss` or `inconclusive`.
These manifests are independent of the rebuildable session database. Child-process
overrides never alter parent environment or global configuration. To disable the
experiment, launch the agent normally with your existing settings.

Codex generates its own protocol schema before an experimental launch. Unsupported
keys block that profile; no UI keystroke injection or transcript rewriting is used.
The installed 0.153.4 schema exposes `model_auto_compact_token_limit`, its `total`
scope, and `compact_prompt`. Key support does not prove that every backend honors
the custom prompt. Supply the actual model window instead of copying the example.
Claude 2.1.259 exposes the append-system-prompt CLI option used for preservation
instructions; its threshold is the documented environment override.

Compare completed tasks of similar scope with the same provider, model and effort.
Record correctness, lost state, recovery reads, token components and compaction
overhead. Keep native behavior unless savings persist without loss of necessary
working state. A 70% or 85% threshold is an experiment, not a recommended default.

References: [Claude caching](https://code.claude.com/docs/en/prompt-caching),
[Claude environment variables](https://code.claude.com/docs/en/env-vars),
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

## Verification

The rebuilt working database contains 723 sessions and 142,374 context events;
SQLite integrity checking returned `ok`. Original source files were read only.

- Full pytest: 263 passed; one existing Starlette/httpx deprecation warning.
- Focused HTTP/context checks after final template and aggregate changes: 49 passed.
- Ruff lint, Ruff formatting, Pyright and `git diff --check`: passed.
- In-app browser: provider/project filters, local model price refresh, selected
  population (12 matching / 3 displayed / 1 analyzed), scenario calculation and
  native support page verified. CSS cache version updated and page overflow fixed.
- Profile subprocess behavior is mocked in tests. No external LLM analysis or
  real compaction experiment was performed; continuation-quality results remain
  required before adopting an experimental threshold.
