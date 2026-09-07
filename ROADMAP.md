# ROADMAP - Local Watchdog

Agreed on 2026-09-05. A plan for small projects on top of stock harnesses. Each milestone delivers an independently useful result; later milestones are not enabled automatically. Open tasks: [TODO.md](TODO.md). Completed work: [DONE.md](DONE.md). Contracts: [architecture](docs/architecture.md).

Execution order: M1-M4 for Codex coding only -> M-Anthropic / WD-022a+b -> App Server / WD-015 -> final cross-platform validation / M5. Anthropic support does not block the Codex observation, analysis, or intervention milestones. Ordinary chats are outside product scope.

## M0 - Foundation and verifiable design

**Status:** complete. Local acceptance checks, including installation of the built wheel in an isolated environment, passed. See [verification](docs/verification.md). Remote CI and live provider checks are not claimed; live integration work starts at M1 / WD-002.

**Outcome:** documentation and an installable Python foundation; no runtime yet.

- README with an accurate status, shared AGENTS/CLAUDE rules, MIT license, gitignore, LF/UTF-8, pyproject/uv.lock, and src layout.
- Conventional Commits, feature branches, behavioral TDD; Ruff/pytest and CI on three operating systems. ty is included with the WD-003 contracts.
- Define project identity, process lifecycle, delivery semantics, quotas, privacy, and visibility limitations.
- Document sources and a compatibility matrix without substituting documentation links for live evidence.
- Keep all repository text in English. Store only open work in TODO.md and move verified completed entries to DONE.md with their IDs and evidence.

**Acceptance:** CLI help works from the installed package; wheel/sdist build; local tests and Ruff pass. Review CI configuration separately from actual remote runs. TODO includes dependencies and verifiable criteria.

## M1 - Reliable collection without intervention

**Outcome:** hooks from Codex CLI and local desktop coding sessions start the core and persist selected project events, validated on Windows. Keep the Python core and integration design portable; obtaining macOS/Linux hosts and validating compatibility are deferred to M5 / WD-019.

1. Provider-neutral typed envelope with a provider field, SQLite migrations, and synthetic fixtures. Keep a small adapter boundary; do not build a general plugin framework before a second implementation is needed. Registry/UUID, worktree resolution, and non-Git roots. Configuration lives outside the checkout.
2. Atomic inbox, idempotent processing, single writer, OS lock, detached launch, pause/start/stop/status. Crash/replay, concurrent startup, partial files, and quota accounting.
3. A lightweight Codex command adapter; normalization aware of versions and capabilities. Provider-specific no-op responses, fail-open behavior, bounded input/latency, and no model feedback.
4. Installer with dry-run/backup/uninstall that preserves existing hooks. Native trust procedures remain mandatory and must use the profile active on the target surface. WD-006 implements explicit JSON file edits; WD-007 adds content capture/redaction, enabled by default with project opt-out. Ordinary tests never install into active provider configuration.
5. Retention of 30/180 days, 2 GiB/project, and bounded inbox/logs; pin, reserve, degraded state, and loss counters. Redact known secrets before persistent writes.
6. `project add/list/remove/relocate`, `daemon start/run/stop/pause/status`, `hooks install/uninstall`, `doctor`, and `sessions list/show --project`. Remove disables collection; only a separate purge deletes data. WD-005 implements lifecycle controls as a single-slot desired-state file; general mutation requests wait for their actual consumers.

**Acceptance:** multiple concurrent Codex sessions/worktrees share one core without mixing sessions/agents. Crash replay does not duplicate envelopes. Harness exit does not terminate the detached core on tested operating systems; document a user-autostart fallback for environments that prohibit detachment. A subsequent hook does not cancel pause. On overflow/corruption/database unavailability, the harness continues and losses remain visible. Measure p95 hook latency and idle overhead. WD-008 records the Python baseline; WD-024 implemented the user-selected Rust adapter while preserving the Python core and inbox contract, then closed by user decision with its provider-controlled Windows latency and unavailable Codex triggers explicitly recorded. WD-027 made that adapter spool-and-forget (redact and spool only; the daemon resolves, builds, and admits), cutting the synthetic concurrent four-caller p95 from 96 to 65 ms. WD-026 owns any future attempt to resolve the actual Codex Windows hook-runner performance. Run live smoke tests for the available Windows Codex CLI/desktop coding surfaces separately from CI fixtures. Access to other operating systems is not an M1 acceptance dependency.

## M2 - Analytics and a manual optimization loop

**Outcome:** the first complete analytical release for Codex coding sessions; behavioral and token/context metrics have equal priority.

1. **WD-009 (complete 2026-09-07).** Codex rollout-v1 transcript reader with persisted offsets, partial lines/rotation, native ID reconciliation, correct cumulative usage counters, and durable reader gaps. Reader failure does not stop hooks.
2. **WD-010 (complete 2026-09-07).** Read-only timeline and durations, tool repetitions/errors, comparable pytest/JUnit results, output size, compactions, usage/cached tokens, versioned evidence IDs, and debounced Git diff fingerprints with uncertain attribution explicitly marked.
3. Deterministic shadow findings: at least three repetitions, identical errors/comparable test sets, and diff oscillation. Evidence, versioned rules, and unknown coverage. No automatic stall verdict based on missing events.
4. **WD-011 (complete 2026-09-07).** CLI `label`, `pin`, `export`, and `purge`; label/pin/purge are acknowledged control requests to the core. Read-only reports and exports work offline.
5. **WD-011 (complete 2026-09-07).** Export selected sessions with a manifest, JSONL, Markdown, and a manual LLM prompt: typical tasks, costly patterns, and candidates for helper/skill/instruction improvements. Review content before sharing it.
6. Manually label 20-50 real sessions: outcome, progress/slow/stuck/externally blocked, and finding correctness. The session count is a calibration target, not proof of representativeness.

**Acceptance:** replay/enrichment does not double usage; unknown is not displayed as zero. Reports are reproducible from fixtures. Incremental readers preserve partial tails and report unsupported formats. Users can select, label, and export a session for manual LLM analysis without network access. Measure overhead and finding quality; list false positives. No eval runner or automatic benchmark task generation.

## M3 - Limited opt-in guidance

**Outcome:** add advisory feedback at a supported Codex hook boundary only after collecting the M2 baseline.

- Separate delivery policy: evidence, capability, expiry, cooldown, and at most one nudge per blocker until new evidence appears. Observe remains the default mode.
- Select rules using labeled traces. Release gate: at least 90% precision on a declared dataset, with counts and uncertainty, plus a review of false signals. A small sample does not justify a hard gate.
- Record proposed/delivered/response observed; delivery is not acceptance of advice. Provide a kill switch and per-project opt-in.
- Compare time, tool calls, outcome quality, and overhead against the baseline. Do not automatically change models, skills, or AGENTS in monitored projects.

**Acceptance:** observe behavior is unchanged; feedback has no recursion or endless Stop continuation; repeated blockers do not spam; disabling feedback stops delivery immediately. Do not promise forced interruption through a textual nudge.

## M4 - Semantic judge and controlled escalation

**Outcome:** optional bounded semantic analysis and read-only second opinions for Codex coding sessions, followed by a human gate only on a demonstrably supported control path.

- Make a separate LLM execution decision before implementation: M0-M3 do not enable external model calls. Require project opt-in, budget, timeout, exclusion of the product's own analytical sessions, and provenance.
- Use a compact facts/failed-attempts/evidence bundle and structured `progress|uncertain|stuck|blocked` output. Do not treat a summary as objective verification.
- Escalation ladder: advisory -> replan request -> read-only second opinion -> question for the user. Do not apply every step to every signal.
- Distinguish model-mediated questions from actual pause/resume. Never automatically grant approvals. Actions are limited by the current adapter's capabilities.

**Acceptance:** budget exhaustion, timeout, or model failure does not break the harness; no recursive analysis; measure false escalation and rescue rates alongside task outcomes. Approve exact policy defaults using M2/M3 results before implementing M4.

## M-Anthropic - Independent Claude coding support

**Schedule:** split into **WD-022a** (Claude observation — **complete 2026-09-06**, see [DONE.md](DONE.md); did not block M1-M4) and **WD-022b** (enrichment, lifecycle beyond observation, guidance/control; after M4 / WD-014, before App Server / WD-015).

**Outcome:** Claude Code CLI and local desktop Code use the established core, storage, reports, and applicable intervention policies. Ordinary chats and Cowork remain excluded.

**WD-022a - observation (done).** Provider-parameterized Python and Rust adapters, a safe `settings.json` / `settings.local.json` installer emitting exec-form entries with `timeout: 2`, the 12-event Claude map, and empty-stdout no-op, with offline tests. Checkout identity resolves from filesystem reads (`git` spawned only for unrecognized layouts), which removed the per-hook subprocess and the concurrent `SessionStart` drop; `disk::fault` makes any nonzero loss counter attributable. Live gate met: interactive `durationMs` for `Stop`, a four-concurrent `claude -p` Pass A with no drop and zero losses across a daemon restart, and a supervised desktop pass with a real composer submission producing a stored `turn.start`. Criterion 1 is judged on Watchdog's own blocking cost (launch benchmark four-caller p95 96 ms) and, by the plan's recorded limitation, excludes Claude's hook-dispatch overhead. Content capture stays the four Codex-equivalent fields. Evidence: [verification](docs/verification.md#wd-022a-claude-observation-gate-met).

**WD-022b.**
1. Refresh native trust/reload behavior and any per-event capability gaps not covered by WD-022a.
2. Add versioned Claude transcript enrichment and the wider content-capture surface (`error`, `duration_ms`, `is_interrupt`). Reuse the shared contracts.
3. Apply the WD-005 process-lifecycle protocol to Claude CLI and desktop, including child survival after actual harness exit.
4. Validate simultaneous Codex/Claude sessions and worktrees, isolation, retention, usage accounting (no cumulative double counting), and no-op responses.
5. Validate advisory/control delivery separately against the M3/M4 policies. Async hook delivery is a separately recorded change, only after the synchronous baseline is fixed and recorded; verify backgrounded-hook durability with the WD-022a evidence standard. Unsupported control capabilities remain disabled.

**Acceptance:** offline adapter tests and Windows live evidence, preserved existing hooks, bounded overhead, independent core lifetime, and an explicit event/control capability matrix. Claude-specific failures do not break Codex. macOS/Linux host validation stays in final M5 / WD-019.

## Optional extensions after M-Anthropic

- **WD-015:** Codex App Server spike, scheduled after WD-022b. Investigate event/control contracts and ownership/attach for existing sessions. Add an adapter only after confirmation; retain hooks. If integration requires launching a harness itself, document a separate mode. M4 remains bounded by verified hook capabilities and does not depend on this extension.
- **WD-016:** a shared read-only project overview without merging databases; later, a local web UI if needed. Evaluate only after a useful M2 release.
- A catalog/eval runner and automated skill improvement only if M2 manual export no longer meets workflow needs.
- MCP adviser only for a real agent-query use case, not as a process startup placeholder.

## M5 - Final cross-platform validation

Deferred compatibility work in the final milestone, not a dependency of M0-M4 or M-Anthropic:

- **WD-019:** obtain access to macOS/Linux hosts and verify installation, hook execution, detached process survival, locking, paths, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python implementation and existing probes; fix platform differences only when demonstrated. Record verified versions and evidence, or explicit surface unavailability. Until then, mark these platforms as unverified rather than blocking earlier milestones.

## Release and verification

- Implement through feature branches and task-scoped commits. Installing the Python package never changes harness configuration: connection is a separate explicit command.
- Unit/contract/integration tests run offline; live vendor smoke tests are a separate manual procedure with sanitized results.
- CI: Windows/macOS/Linux, minimum Python 3.12 plus 3.13 on Linux. Claim newer Python support only after testing. Record actually verified versions at each milestone. Retain the existing CI matrix as early feedback; dedicated macOS/Linux host access and live compatibility acceptance belong to M5 / WD-019.
- Storage upgrades: versioned migrations and recovery tests; installer upgrade/uninstall preserves existing configuration. Do not leave test-owned daemons running.
- M3-M5 intentionally include research/decision gates: these are future milestones, not permission to silently choose control or LLM policy.
