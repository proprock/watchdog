# Software factory alignment audit

Date: 2026-09-07

Scope: WD-103 report-only audit of the repository against
`uber-efficient-software-factory-codex-claude-research.md`.

## Executive verdict

Watchdog has a credible, safety-conscious observation substrate and the beginning of a
manual trace-analysis loop. Its strongest evidence covers local event collection,
durable delivery, storage recovery, bounded native-hook overhead, deterministic shadow
findings, and offline report/export behavior. That is meaningful implementation, not
only a design document.

It is not yet the closed-loop software-factory watchdog described by the research
article. In particular, it does not measure progress against task acceptance criteria,
has not calibrated its findings on labeled real sessions, and does not implement a
semantic judge, staged guidance, a human gate, benchmark-driven runtime model routing,
or a skill-improvement loop. The current product should therefore be described as an
external observer with deterministic shadow analysis, not as a proven stuck detector or
realtime supervisor.

The repository roadmap is directionally sound: observe first, label real sessions,
calibrate rules, add opt-in guidance, and only then consider semantic and human
escalation. The principal risk is advancing to intervention before closing the evidence
gaps in report realism and calibration.

## Scope and proof boundary

This audit changes documentation only. It makes no public API, CLI, configuration,
schema, hook, or runtime behavior change. It does not install hooks, call an LLM, run a
live provider probe, or modify the untracked research article. Recommendations below
are proposed follow-up work, not authorization to change a monitored harness.

Repository scope and accepted decisions come from `README.md:3-16` and
`docs/architecture.md:5-69`; milestone intent comes from `ROADMAP.md:34-105`. The
research article is analytical input. It is not implementation evidence, as already
stated in `docs/integrations.md:12`.

## Evidence taxonomy

Evidence is weighted in this order. A lower tier can prove a local contract but cannot
substitute for a higher-tier claim.

| Evidence class | What it can prove | Current examples | Important limit |
|---|---|---|---|
| live-provider | An installed provider version emitted a callback and the production path handled it on the observed surface | Windows Codex and Claude observations in `docs/provider-compatibility.md:15-32`; Claude gate in `docs/verification.md:125-184` | Sparse, version- and surface-specific; it does not prove every callback, platform, report rule, or end-to-end task outcome |
| practical offline integration | Multiple production components work together with real files, processes, Git repositories, SQLite, or an installed wheel | `tests/test_rust_adapter.py::test_native_spools_then_daemon_resolves_and_admits`, `::test_copied_native_binary_starts_python_core_and_preserves_worktrees`; storage crash/replay tests; installed-wheel checks recorded in `docs/verification.md:336-359` | No provider scheduler or native callback payload is involved |
| synthetic benchmark | Performance and loss behavior under a declared controlled workload | `docs/evidence/wd027-spool-bench.json`; WD-008/024 measurements summarized in `docs/cli.md:115-157` and `TODO.md:27` | Excludes provider scheduling and may exclude the actual shell runner; synthetic success is not native-hook SLO success |
| fixture/contract simulation | Deterministic parsing, mapping, and failure behavior for known shapes | `tests/fixtures/hooks/README.md:1-9`, `tests/test_analysis.py`, `tests/test_transcripts.py`, Python/Rust parity tests | Hand-authored shapes and timestamps do not prove real-world frequency, representativeness, or detection quality |
| docs/config | Intended scope, invariants, sequencing, and configured CI | `TENETS.md`, `AGENTS.md`, `ROADMAP.md`, `docs/*.md`, CI configuration | Intent is not runtime behavior; configured CI is not a successful remote run |

## Evidence-weighted maturity matrix

The only maturity statuses used below are: **verified**, **implemented but partially
proven**, **documented only**, **intentionally deferred**, and **missing or
misdirected**.

| Capability | Status | Evidence-weighted assessment |
|---|---|---|
| Model routing | documented only | `AGENTS.md:7-17` prescribes workload-based sol/luna/terra/astra routing and asks that aggregate cost and rework be evaluated. No repository benchmark, Pareto frontier, eval harness, or cost-per-correct-task dataset proves those choices. These are development-agent instructions, not Watchdog runtime routing. |
| Context/tool efficiency | implemented but partially proven | The native adapter was reduced to bounded projection, redaction, atomic spool, and detached-core recovery (`native/src/main.rs::observe`, lines 190-250). Reports count retained output bytes, compactions, duration, and usage (`src/agent_watchdog/analysis.py::analyze`, lines 157-327). Synthetic launch measurements show improvement. Tool search, code-mode batching, compaction tuning, request/turn accounting, a context broker, and prompt-cache policy are absent from the product. |
| External stateful watchdog | verified | Native hook event delivery through durable spool, daemon, project inbox, and SQLite has practical offline integration coverage and bounded Windows live-provider evidence. Daemon ownership, crash recovery, pause, replay, and storage failure isolation are covered by production-path tests and verification records (`docs/architecture.md:17-35`, `docs/verification.md:61-188`, `:336-426`). The verified scope is observation, not realtime control. |
| Deterministic progress signals | implemented but partially proven | `src/agent_watchdog/analysis.py::analyze` implements repeated exact tool input/outcome, exact structured failure, comparable pytest/JUnit failure-set, output/usage/compaction metrics; `diff_oscillations` adds A-to-B-to-A signals. Offline tests prove deterministic behavior, but no labeled real-session dataset proves precision, recall, or that the signals represent progress or a stall. The implementation intentionally emits no stall verdict. |
| Calibration | intentionally deferred | WD-012 requires 20-50 manually labeled real sessions, false-positive review, overhead measurement, and recommendations (`TODO.md:5-8`; `ROADMAP.md:43-45`). No completed calibration report exists. |
| Semantic judge | intentionally deferred | WD-014 requires a separately accepted LLM/control design, opt-in, budget, isolation, timeout, and structured `progress|uncertain|stuck|blocked` output (`ROADMAP.md:60-69`; `TODO.md:11-12`). No automatic LLM call exists, consistent with `TENETS.md:41-44`. |
| Staged intervention | intentionally deferred | WD-013 and WD-014 describe opt-in advisory feedback, cooldown, replan, read-only second opinion, and a human gate. Current adapters explicitly emit only provider no-op responses and do not inject context or control (`docs/hooks.md:130-158`, `:187-220`). App Server attach/steer/interrupt remains the WD-015 research gate. |
| Skill-improvement loop | intentionally deferred | Manual export can identify helper, skill, and instruction candidates (`src/agent_watchdog/inspection.py::export_sessions`, lines 236-327), but it neither runs evals nor changes skills. A real-work routing benchmark and catalog/eval runner are optional only after M2 produces enough labeled exports (`ROADMAP.md:88-93`). |

No matrix row is marked **missing or misdirected** because the absent closed-loop
features are explicitly deferred rather than accidentally claimed complete. Individual
implementation blind spots below are missing coverage within otherwise implemented
rows.

## Traced implementation paths

### Native hook path

```text
provider callback
  -> native/src/main.rs::observe
     bounded stdin -> redact fixed projection -> atomic data/spool/*.json
     -> native/src/main.rs::ensure_daemon
  -> src/agent_watchdog/daemon.py::_drain_spool
     validate -> resolve repository/worktree -> build_envelope -> _admit
  -> project Inbox.publish
  -> src/agent_watchdog/daemon.py::_poll
     Inbox.drain -> storage.Store.put -> SQLite
  -> src/agent_watchdog/inspection.py::report / export_sessions
     read-only snapshot -> analysis.analyze -> JSON report or manual export bundle
```

The key symbols are `native/src/main.rs::observe` (190-250),
`src/agent_watchdog/daemon.py::_drain_spool` (381-466), `_poll` (480-552),
`src/agent_watchdog/storage.py::Store.put`, and
`src/agent_watchdog/inspection.py::report` (183-233). Practical offline coverage
includes `tests/test_rust_adapter.py::test_native_spools_then_daemon_resolves_and_admits`,
`tests/test_daemon.py::test_spool_record_is_resolved_built_and_admitted`, and
`::test_spool_redrain_is_idempotent`.

### Python fallback path

The Python fallback does not currently follow the same spool-and-forget path:

```text
src/agent_watchdog/cli.py::main
  -> src/agent_watchdog/hooks.py::observe
     load config -> resolve checkout -> build envelope -> daemon.enqueue
  -> project Inbox
  -> daemon _poll -> Inbox.drain -> SQLite
```

`src/agent_watchdog/hooks.py::observe` (122-163) performs configuration loading,
registry resolution, envelope construction, sanitization, and inbox enqueue in the hook
process. This differs from the generalized hook path drawn in
`docs/architecture.md:10-19` and the wording around `agent-watchdog hook` in
`docs/hooks.md:94-128`. The native adapter documentation and code agree; the Python
fallback needs either explicitly separate documentation or a future parity decision.

**Resolved by WD-110 (2026-09-16):** the ambiguity is closed by removing the second
production path rather than reconciling it with the first. `hooks install` now
requires a native adapter (`hook_install.change(..., require_adapter=True)` from
`cli.py`); omitting one is a clear installation error, never a silent fallback
selection. `hooks.py::observe` and the CLI's `hook` subcommand remain, but only
as an explicitly non-production developer/test utility — see `docs/architecture.md`
("Processes and delivery") and `docs/hooks.md` for the current, disambiguated
wording, and [DONE.md](../DONE.md) for verification evidence.

### Transcript enrichment

`src/agent_watchdog/transcripts.py::source_from_hook` (40-53) accepts only an absolute
Codex transcript path from a normal hook event with a session identity. The daemon calls
`transcripts.enrich` after draining a project's inbox. `_process_source` validates the
session metadata, maintains device/inode/offset/partial-tail state, accepts versioned
token usage records, derives fieldwise deltas, emits durable observation gaps, and stores
new usage envelopes. The behavior has substantial fixture/contract coverage in
`tests/test_transcripts.py`, but WD-009 records no live provider probe
(`docs/verification.md:37-59`).

### Fingerprints and diff snapshots

- `analysis.analyze` hashes the exact canonical tool name, captured input, and parsed
  outcome. Failure fingerprints include the exact canonical response and optional exit
  code. Pytest/JUnit extraction is implemented by `analysis._test_failures` (77-86).
- `daemon._capture_diff_snapshot` (37-54) takes a debounced, bounded Git read after
  admission. `analysis.git_diff_fingerprint` (102-124) hashes
  `git diff --no-ext-diff --no-textconv --binary --`. It therefore observes unstaged
  tracked changes only; staged/index changes and untracked files are outside the signal.
- `analysis.diff_oscillations` (127-154) detects adjacent A-to-B-to-A hashes per checkout
  and always marks attribution uncertain.

### Labels and exports

Label, pin, and purge commands submit bounded control requests to the daemon's single
writer. Reports apply the provider/session label on read. The stored label shape is
currently `task_outcome` plus optional free-form `task_type`
(`src/agent_watchdog/inspection.py::_label`, lines 32-45). `export_sessions` writes the
selected events, summary, versioned manifest, and manual analysis prompt without calling
an LLM. It preserves the content-review warning and treats trace data as untrusted.

## Code versus document findings

| Finding | Code evidence | Document/claim comparison | Assessment |
|---|---|---|---|
| Native and Python adapter paths differ | `native/src/main.rs::observe` spools only; `src/agent_watchdog/hooks.py::observe` resolves and enqueues directly | `docs/architecture.md:10-19` presents both hook commands as spool adapters; `docs/hooks.md:94-128` starts with a native qualification but then describes `agent-watchdog hook` as if the spool path were universal | Documentation ambiguity and operational parity gap; not evidence that either path is broken. **Resolved by WD-110 (2026-09-16):** the Python path is no longer a second production path; docs updated to disambiguate the two by name |
| Artifact storage is not wired to production hook ingestion | Production callers of `Store.put` are inbox draining and transcript enrichment; production search found no `artifacts=` caller. The canonical envelope is inserted into SQLite up to the payload limit | `docs/architecture.md:33` says large raw outputs are stored as separate redacted artifacts; `Store._put` supports that capability | Implemented storage capability, but not current ingestion behavior. This is a context/storage-efficiency mismatch and should not be claimed as automatic offloading |
| Error identity is exact, not normalized | `analysis.analyze` fingerprints the full canonical failing response and exit code | The research article proposes normalization of timestamps, paths, UUIDs, ports, and addresses; current docs more cautiously say identical or structured error | Deterministic and reproducible, but fragile to incidental output changes; calibrate before broadening normalization |
| Tool outcome availability is provider-shape dependent | `analysis._outcome` recognizes a nested exit code or `isError: true`; otherwise it returns `unknown` | Live Codex evidence recorded opaque string responses and explicitly avoided a universal exit-code parser (`docs/provider-compatibility.md:34-42`) | The main report tests use structured dictionaries and therefore prove less than live-provider effectiveness |
| Test-set comparison is intentionally narrow | `_test_failures` requires `pytest` in the captured command for pytest parsing, uses limited regexes, and the final signature includes the exact command | `docs/architecture.md:60-61` intentionally requires comparable commands and warns against aggressive normalization | Sound safety bias, but it misses logically equivalent invocations and non-pytest/JUnit tools |
| Diff fingerprint omits relevant working-tree state | `git_diff_fingerprint` invokes plain `git diff`, excluding staged and untracked content | Documentation says Git diff fingerprint without defining index/untracked coverage; the research target is general diff oscillation | Deterministic signal blind spot, not necessarily a bug. Future work must define the intended state before changing it |
| Calibration labels are incomplete for WD-012 | `SessionLabel` stores only task outcome and task type | `ROADMAP.md:43` calls for `progress/slow/stuck/externally blocked` and finding-correctness labels | WD-012 needs an explicit annotation workflow or schema decision before data collection; do not overload task type silently |
| Output byte count is retained-payload size | `analysis.analyze` counts UTF-8 bytes of the canonical captured response | The report calls it output size; content may be absent, redacted, bounded, or provider-shaped | Useful local metric, but not proof of complete provider output volume or context-token cost |
| Factory economics are not yet measured | Reports expose event counts, tool duration, compactions, output bytes, and token deltas | The article prioritizes turns/session, requests/turn, tokens/request, and cost per correctly completed task | Partial observability only; no cost model, request accounting, or correctness-denominated benchmark exists |

## Test realism audit

### Strong areas

- Native-to-core integration uses a built/copied Rust executable, real spool files,
  repository/worktree resolution, a detached Python core, and SQLite. Relevant tests are
  `tests/test_rust_adapter.py::test_native_spools_then_daemon_resolves_and_admits`,
  `::test_copied_native_binary_starts_python_core_and_preserves_worktrees`, and
  `::test_native_spooled_cwd_resolves_like_the_registry`.
- Delivery and storage tests exercise atomic publication, replay identity, partial
  writes, one-writer ownership, process crash before/after commit, quota pressure,
  simulated disk-full behavior, retention, and migration. See
  `tests/test_storage.py::test_process_crash_replays_without_duplicates`,
  `tests/test_retention.py::test_disk_full_keeps_queue_empty_and_loss_survives_reopen`,
  and `::test_concurrent_publish_obeys_inbox_quota`.
- Adapter contract tests check all documented Claude event mappings, silent Claude
  stdout, Codex no-op JSON, redaction, content opt-out, unknown event preservation, and
  Python/Rust envelope parity. These are strong contract simulations, not live proof.
- Checkout resolution is compared against real Git layouts, and installer tests preserve
  unrelated settings, ownership records, recovery, and shell metacharacters.
- Live evidence separately covers bounded Windows Codex and Claude callbacks. The
  strongest Claude run observed 11 of 12 configured native events; `Notification`
  remained unobserved (`docs/verification.md:145-184`).

### Four known limited-evidence test areas

| Test | What it proves | Why evidence is limited |
|---|---|---|
| `tests/test_cli.py::test_help_describes_bootstrap_without_starting_collection` (5-15) | The module entry point returns success and argparse help contains the exact current command-list string | It does not assert that an isolated `--home` remains untouched, so the test name's no-start/no-state-change implication is broader than its assertions; the exact list is also formatting-coupled |
| `tests/test_claude_hooks.py::test_fixture_covers_all_twelve_claude_events` (58-59) | The local fixture event names equal the local `KINDS` mapping | This is self-consistency between two repository-maintained inputs, not independent evidence that the current Claude product emits all events or fields |
| `tests/test_benchmark_hooks.py::test_benchmark_waits_for_live_pid_after_status_invalidation` (19-26) and `::test_benchmark_rejects_stale_daemon_and_bounds_readiness` (29-38) | The benchmark helper polls through startup states and raises at its modeled deadline | Both command results and time/sleep are mocked, so they prove polling logic, not readiness of a real daemon or the absence of process races |
| `tests/test_hook_stream_timing.py::test_capture_writes_stamped_hook_rows_without_content` (65-139) | The capture parser stamps selected hook/tool/run rows and omits supplied prompt/path content | It feeds a hand-authored stream through a mocked `Popen`; it does not prove the current Claude CLI stream shape, flush timing, callback completeness, or process behavior |

These tests remain useful within their stated boundaries. Separately,
`tests/test_analysis.py` and the report case in `tests/test_observation_cli.py` are sound
deterministic contract tests, but their hand-built envelopes, responses, fingerprints,
and timestamps cannot establish real-session detection quality, prevalence, precision,
or recall.

The current verification also exposed a test-harness sensitivity: using a repository-
local `.cache` directory as pytest's base temporary directory places generated project
roots inside the enclosing Watchdog Git checkout. Registry and hook tests then resolve
those roots to the enclosing checkout instead of the temporary registered project. Two
such attempts failed broadly (69 failed / 62 passed, then 75 failed / 203 passed); the
same focused and full suites passed with a base temporary directory outside every Git
checkout. This is evidence about test-environment portability, not a product regression.

## Missing end-to-end scenarios

1. A real Codex callback traverses native adapter -> spool -> daemon -> SQLite and then
   produces a repeated-error/test finding through `agent-watchdog report` and a complete
   export bundle. WD-010 and WD-011 explicitly ran no live provider probe.
2. ~~The Python fallback and native adapter process equivalent events through their
   different delivery paths and produce equivalent stored envelopes, reports, gaps, and
   loss diagnostics under failure.~~ Moot since WD-110 (2026-09-16): the Python path is
   no longer a second production delivery path to keep at parity with the native one.
3. Real opaque Codex and Claude tool responses produce useful outcomes, error identities,
   and comparable test sets, including missing output and `capture_content = false`.
4. Staged, unstaged, untracked, renamed, and concurrent multi-agent changes exercise a
   deliberately specified working-tree fingerprint contract.
5. A real Codex rollout file exercises transcript enrichment through append, partial
   tail, restart, rotation/truncation, and cumulative counter reset without duplicate
   usage. Current coverage is fixture/contract simulation.
6. A calibration workflow records task outcome, progress class, external blocking,
   finding correctness, and false negatives for 20-50 real sessions with an auditable
   denominator.
7. Simultaneous live Codex and Claude sessions/worktrees demonstrate provider isolation,
   event completeness, usage accounting, and bounded overhead.
8. Actual Codex Windows runner latency meets the documented target or is closed as a
   versioned external limitation. WD-026 retains the observed 279 ms sequential / 563 ms
   four-way PowerShell result versus 65 / 173 ms direct Rust (`TODO.md:27`).
9. macOS/Linux installation, launch, lock, path, cleanup, and provider behavior remain
   intentionally unverified under WD-019.
10. Future M3/M4 work must test kill-switch immediacy, cooldown/hysteresis, duplicate
    nudge suppression, non-recursive judging, budget/timeout failure, false escalation,
    model-mediated question versus actual pause, and user resume. None is a current
    implementation claim.

## Recommendations

### P0 - calibrate before guidance

1. Resolve the WD-012 annotation model before labeling sessions. Progress class,
   external block, finding correctness, and task outcome need distinct auditable fields
   or an explicit external annotation artifact.
2. Label the planned 20-50 real sessions and publish counts, coverage, false positives,
   false negatives, and examples of unavailable evidence. Do not treat the sample as
   representative beyond its declared tasks and provider versions.
3. Use the calibration data to decide whether error normalization, logical-command
   equivalence, more test parsers, or acceptance-delta tracking improve precision. Avoid
   copying article thresholds without measured benefit.
4. Define measurable factory metrics available from the harness: turns/session,
   response or request counts where observable, tokens, tool calls, wall/active time,
   human interventions, and labeled outcome. Keep monetary estimates unavailable unless
   a sound pricing/subscription model exists.

### P1 - make practical and manual evidence trustworthy

1. ~~Reconcile documentation with the two real adapter paths. State explicitly that the
   native adapter is spool-and-forget and the Python fallback currently resolves and
   enqueues directly, or create a separately approved parity task.~~ Done via WD-110
   (2026-09-16): rather than reconciling two production paths, the Python one was
   removed from production (`hooks install` now requires a native adapter) and the
   docs updated to name the two by their actual distinct purposes.
2. Correct the large-output claim: document artifact storage as an available storage
   capability until production ingestion actually offloads large hook content. Do not
   imply that persisted SQLite envelopes are fingerprint-only.
3. Add one practical offline end-to-end report/export scenario driven through the built
   native adapter, spool, daemon, SQLite, and CLI. Use provider-realistic opaque and
   missing fields; no live account is required.
4. Define the diff state contract—unstaged tracked only, staged plus unstaged, or a wider
   working-tree inventory—and add focused coverage before changing fingerprint inputs.
5. Use the WD-102 result record for future manual/live probes so provider dispatch,
   adapter start/failure, missing callback, retries, persisted losses, end-to-end
   delivery, provenance, and cleanup are reported separately.

### P2 - measure routing and skills before closing the loop

1. Build a real-work model-routing benchmark only after labeled exports exist. Compare
   correctness, completed-task cost, latency, and rework instead of treating the current
   development-agent routing policy as proven.
2. Turn recurring, calibrated failure fingerprints into candidate helper/skill changes,
   then evaluate them with with/without and cost-per-correct-task comparisons. Keep this
   optional until the manual export workflow demonstrates a real bottleneck.
3. Implement M3 as per-project opt-in advisory delivery with evidence, expiry, cooldown,
   one nudge per blocker, delivery receipts, and a kill switch. Preserve observation as
   the default.
4. Gate M4 semantic judging behind deterministic evidence, explicit data-sharing policy,
   budget, timeout, provenance, and recursion exclusion. Measure rescue and false
   escalation rates against labeled outcomes.
5. Treat read-only second opinion and a structured human question as separate stages.
   Do not describe a textual Stop continuation as a hard pause/resume mechanism.
6. Run WD-015 before claiming realtime Codex supervision. Verify ownership/attach for
   existing sessions and the actual steer/interrupt contract rather than relying on App
   Server documentation.

## Current audit verification

This table records results from the root integration run. It does not infer a
live-provider pass from offline checks.

| Check | Result | Evidence/notes |
|---|---|---|
| Review `docs/software-factory-audit.md` against current code and source-of-truth docs | PASS | Graph-guided trace plus direct review of the cited implementation, tests, history, and evidence boundaries |
| `cargo build --release --locked --manifest-path native/Cargo.toml` | PASS | 5.14 s; warning only: could not canonicalize `C:\Users\tonym` |
| Focused offline suite with OS-external base temp | PASS | 131 passed in 38.09 s |
| Full offline suite with OS-external base temp | PASS | 278 passed in 47.82 s |
| Direct native synthetic benchmark | PASS, synthetic only | Sequential p95 65.1 ms; concurrent-four p95 355.3 ms; 41 events, restart preserved, zero losses; `native_provider_validation=false` |
| PowerShell native synthetic benchmark | PASS, synthetic only | Sequential p95 385.0 ms; concurrent-four p95 1032.3 ms; 41 events, restart preserved, zero losses; `native_provider_validation=false` |
| `git diff --check` | PASS | Root integration result |
| Local links introduced by WD-103 | PASS | A repository-wide scan also found the pre-existing missing `tmp-WD-024.md` target referenced from `DONE.md` and `docs/verification.md`; WD-103 did not alter those historical references |
| Confirm WD-103 tracked scope is only `docs/software-factory-audit.md`, `TODO.md`, `ROADMAP.md`, and `DONE.md` after root integration | PASS | Final status/diff review; the untracked research article remains out of scope |
| Live provider probes | NOT RUN | Explicitly outside WD-103 report scope |

Two OS-temporary pytest roots remained because verified cleanup attempts, including an
elevated retry, returned access denied. The paths are reported in the task closeout; no
Watchdog daemon or provider hook was intentionally left running by this audit.

## Bottom line

Watchdog has implemented the difficult safety and durability prerequisites for an
external observer. It has not yet demonstrated that its deterministic findings identify
real stalls, nor has it implemented the intervention half of the feedback loop. The
next evidence-bearing milestone is P0 / WD-012 calibration, with the P1 truthfulness and
end-to-end realism work preserving the credibility of its inputs. Only measured results
should unlock guidance, semantic judging, human escalation, or automated skill
improvement.
