# ROADMAP - Local Watchdog

Agreed on 2026-09-05. A plan for small projects on top of stock harnesses. Each milestone delivers an independently useful result; later milestones are not enabled automatically. Open tasks: [TODO.md](TODO.md). Completed work: [DONE.md](DONE.md). Contracts: [architecture](docs/architecture.md).

## M0 - Foundation and verifiable design

**Status:** complete. Local acceptance checks, including installation of the built wheel in an isolated environment, passed. See [verification](docs/verification.md). Remote CI and live provider checks are not claimed; live integration work starts at M1 / WD-002.

**Outcome:** documentation and an installable Python foundation; no runtime yet.

- README with an accurate status, shared AGENTS/CLAUDE rules, MIT license, gitignore, LF/UTF-8, pyproject/uv.lock, and src layout.
- Conventional Commits, feature branches, behavioral TDD; Ruff/pytest and CI on three operating systems. Add ty when contracts are introduced.
- Define project identity, process lifecycle, delivery semantics, quotas, privacy, and visibility limitations.
- Document sources and a compatibility matrix without substituting documentation links for live evidence.
- Keep all repository text in English. Store only open work in TODO.md and move verified completed entries to DONE.md with their IDs and evidence.

**Acceptance:** CLI help works from the installed package; wheel/sdist build; local tests and Ruff pass. Review CI configuration separately from actual remote runs. TODO includes dependencies and verifiable criteria.

## M1 - Reliable collection without intervention

**Outcome:** hooks from both harnesses start the core and persist selected project events, validated on Windows. Keep the Python core and integration design portable; obtaining macOS/Linux hosts and validating compatibility are deferred to M5 / WD-019.

1. Typed envelope, SQLite migrations, and synthetic fixtures. Registry/UUID, worktree resolution, and non-Git roots. Configuration lives outside the checkout.
2. Atomic inbox, idempotent processing, single writer, OS lock, detached launch, pause/start/stop/status. Crash/replay, concurrent startup, partial files, and quota accounting.
3. Lightweight Codex/Claude command adapters; normalization aware of versions and capabilities. Empty stdout, fail-open behavior, bounded input/latency, and no model feedback.
4. Installer with dry-run/backup/uninstall that preserves existing hooks. Native trust procedures remain mandatory. Ordinary tests never install hooks.
5. Retention of 30/180 days, 2 GiB/project, and bounded inbox/logs; pin, reserve, degraded state, and loss counters. Redact known secrets before persistent writes.
6. `project add/list/remove/relocate`, `daemon start/run/stop/status`, `hooks install/uninstall`, `doctor`, and `sessions list/show --project`. Remove disables collection; only a separate purge deletes data.

**Acceptance:** two concurrent harnesses/worktrees share one core without mixing sessions/agents. Crash replay does not duplicate envelopes. Harness exit does not terminate the detached core on tested operating systems; document a user-autostart fallback for environments that prohibit detachment. A subsequent hook does not cancel pause. On overflow/corruption/database unavailability, the harness continues and losses remain visible. Measure p95 hook latency and idle overhead. Run live smoke tests for available Windows CLI/desktop surfaces separately from CI fixtures. Access to other operating systems is not an M1 acceptance dependency.

## M2 - Analytics and a manual optimization loop

**Outcome:** the first complete analytical release; behavioral and token/context metrics have equal priority.

1. Transcript readers for verified formats, persisted offsets, partial lines/rotation, native ID reconciliation, and correct cumulative usage counters. Reader failure does not stop hooks.
2. Timeline and durations, tool repetitions/errors, pytest/JUnit results, output size, compactions, usage/cached tokens. Debounced Git diff fingerprints with uncertain attribution explicitly marked.
3. Deterministic shadow findings: at least three repetitions, identical errors/comparable test sets, and diff oscillation. Evidence, versioned rules, and unknown coverage. No automatic stall verdict based on missing events.
4. CLI `report --project --session|--since --format md|json`, `label`, `pin`, `export`, and `purge`; label/pin/purge are acknowledged control requests to the core. Read-only reports work offline.
5. Export selected sessions with a manifest, JSONL, Markdown, and a manual LLM prompt: typical tasks, costly patterns, and candidates for helper/skill/instruction improvements. Review content before sharing it.
6. Manually label 20-50 real sessions: outcome, progress/slow/stuck/externally blocked, and finding correctness. The session count is a calibration target, not proof of representativeness.

**Acceptance:** replay/enrichment does not double usage; unknown is not displayed as zero. Reports are reproducible from fixtures. Incremental readers preserve partial tails and report unsupported formats. Users can select, label, and export a session for manual LLM analysis without network access. Measure overhead and finding quality; list false positives. No eval runner or automatic benchmark task generation.

## M3 - Limited opt-in guidance

**Outcome:** add advisory feedback at a supported hook boundary only after collecting the M2 baseline.

- Separate delivery policy: evidence, capability, expiry, cooldown, and at most one nudge per blocker until new evidence appears. Observe remains the default mode.
- Select rules using labeled traces. Release gate: at least 90% precision on a declared dataset, with counts and uncertainty, plus a review of false signals. A small sample does not justify a hard gate.
- Record proposed/delivered/response observed; delivery is not acceptance of advice. Provide a kill switch and per-project opt-in.
- Compare time, tool calls, outcome quality, and overhead against the baseline. Do not automatically change models, skills, or AGENTS in monitored projects.

**Acceptance:** observe behavior is unchanged; feedback has no recursion or endless Stop continuation; repeated blockers do not spam; disabling feedback stops delivery immediately. Do not promise forced interruption through a textual nudge.

## M4 - Semantic judge and controlled escalation

**Outcome:** optional bounded semantic analysis and read-only second opinions, followed by a human gate only on a demonstrably supported control path.

- Make a separate LLM execution decision before implementation: M0-M3 do not enable external model calls. Require project opt-in, budget, timeout, exclusion of the product's own analytical sessions, and provenance.
- Use a compact facts/failed-attempts/evidence bundle and structured `progress|uncertain|stuck|blocked` output. Do not treat a summary as objective verification.
- Escalation ladder: advisory -> replan request -> read-only second opinion -> question for the user. Do not apply every step to every signal.
- Distinguish model-mediated questions from actual pause/resume. Never automatically grant approvals. Actions are limited by the current adapter's capabilities.

**Acceptance:** budget exhaustion, timeout, or model failure does not break the harness; no recursive analysis; measure false escalation and rescue rates alongside task outcomes. Approve exact policy defaults using M2/M3 results before implementing M4.

## M5 - Cross-platform validation and optional extensions

Deferred compatibility work, not a dependency of M0-M4:

- **WD-019:** obtain access to macOS/Linux hosts and verify installation, hook execution, detached process survival, locking, paths, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python implementation and existing probes; fix platform differences only when demonstrated. Record verified versions and evidence, or explicit surface unavailability. Until then, mark these platforms as unverified rather than blocking earlier milestones.

Independent optional extensions:

- Codex App Server: investigate event/control contracts and ownership/attach for existing sessions. Add an adapter only after confirmation; retain hooks. If the integration requires launching a harness itself, document a separate mode.
- A shared read-only project overview without merging databases; later, a local web UI if needed.
- A catalog/eval runner and automated skill improvement only if M2 manual export no longer meets workflow needs.
- MCP adviser only for a real agent-query use case, not as a process startup placeholder.

## Release and verification

- Implement through feature branches and task-scoped commits. Installing the Python package never changes harness configuration: connection is a separate explicit command.
- Unit/contract/integration tests run offline; live vendor smoke tests are a separate manual procedure with sanitized results.
- CI: Windows/macOS/Linux, minimum Python 3.12 plus 3.13 on Linux. Claim newer Python support only after testing. Record actually verified versions at each milestone. Retain the existing CI matrix as early feedback; dedicated macOS/Linux host access and live compatibility acceptance belong to M5 / WD-019.
- Storage upgrades: versioned migrations and recovery tests; installer upgrade/uninstall preserves existing configuration. Do not leave test-owned daemons running.
- M3-M5 intentionally include research/decision gates: these are future milestones, not permission to silently choose control or LLM policy.
