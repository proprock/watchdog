# Context structure analysis

WDI reconstructs the observable context immediately before each recorded model
call. The reconstruction is designed for comparing long sessions, identifying
what made context grow, and evaluating compaction outcomes without duplicating
raw session content in the database.

## What is measured

Each normal import emits ordered `context_events` in the same file-reading pass.
Events describe instructions, user and assistant messages, reasoning summaries,
tool inputs, code and file reads, search results, directory listings, patches,
VCS output, build and test logs, web results, and unclassified tool output.
Visible blocks store character and UTF-8 byte counts plus a SHA-256 fingerprint
of normalized content. Tool records retain only the tool name, command kind, and
hashed target. Raw text, commands, paths, and ciphertext are not copied.

Usage events retain the exact counters present in the source session: input,
cache read, cache creation, output, and reasoning tokens, plus the model context
window when available. WDI does not estimate a per-block token allocation. The
dashboard deliberately plots exact call-level tokens beside measured visible
bytes instead of presenting byte ratios as token attribution.

## Reconstruction rules

- Named instruction slots replace their previous value.
- History accumulates until a compaction boundary.
- A visible `replacement_history` becomes the new context base.
- Encrypted reasoning and compact items are recorded as opaque facts with no
  size derived from ciphertext.
- A snapshot is `visible`, `partial`, or `opaque` according to what can be
  reconstructed from the session.
- Normalized fingerprints identify repeated or reintroduced content without
  revealing it.

Codex `compacted` payloads and their paired `context_compacted` marker form one
logical boundary, including when the marker arrives in a later incremental
import. Codex trigger mode remains `unknown` unless a future source format
records it. Claude trigger, pre/post tokens, and duration are retained when
present. The compact prompt that produced an existing session is not recorded,
so WDI evaluates its outcome but cannot compare historical prompt variants.

OpenAI describes compaction items as opaque continuation state, recommends
compacting after major milestones, and requires subsequent prompts to preserve
functionally equivalent instructions. WDI follows those constraints. See the
[OpenAI compaction guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.2).

## Dashboard and advisor

Open **Context Analysis** and press **Run local analysis**. Filters cover provider,
project, date range, minimum calls and turns, and Top N. Rankings are available
for model calls, turns, cumulative input tokens, peak context, observable bytes,
compactions, and repeated bytes. The selected session includes a per-call stacked
composition timeline, exact token overlays, compaction boundaries, growth and
repeat counters, and reconstruction coverage.

The deterministic advisor reports evidence and limitations; it does not infer that
manual or automatic compaction is superior from historical reductions. Repeated
content may be necessary. Native compaction remains the default until continuation
quality and economic benefit are demonstrated in controlled trials.

The analysis population can be all filtered sessions, Top N, or checked rows.
Additional columns separate uncached input, cache reads/writes, output, exact
repetition, partial overlap estimates, changed targets, and error-associated data.
Visible text tokens use approximately four characters per token. They are not
an allocation of measured request usage. Tree streams are shown independently.

Before optional advice, WDI estimates the actual aggregate prompt through the
shared token estimator and PricingResolver. Model and output-budget changes are
local calculations. Unknown tariffs remain unknown. Prices are API equivalents,
with no known conversion to subscription limits.

The local scenario calculator compares continuation, earlier compaction, output
prevention, and avoidable retries. It accounts for retained cache prefix,
summarization, cache rebuilds, future reads, and reacquisition under explicit
assumptions. See the [audit and native-profile guide](context-efficiency-audit.md)
for findings, limitations, experiment commands, and reproduction instructions.

Optional Claude and Codex advisor buttons reuse the coach report cache under a
separate namespace and prompt version. Only aggregate categories, counts, and
ratios are sent; the payload excludes raw text, paths, commands, session IDs, and
project names.

## Operational notes

The schema remains rebuildable. After upgrading, delete `~/.wdi/wdi.sqlite` and
run `uv run wdi import` so existing sessions receive `context_events`. Incremental
imports and snapshot merges include the new table.

Changed streams are replayed to reconcile provider fragments across imports;
unchanged files are skipped. Codex cached input is a subset of input; Claude
input includes separate cache read/write components. Provider message identities
and cumulative usage signatures reconcile repeated observations. Raw counters
and reconciliation provenance remain available in event metadata.

The updated local audit and measured import timings are recorded in
[Context efficiency audit](context-efficiency-audit.md).
