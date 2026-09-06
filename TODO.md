# TODO

Queue: WD-024 (gates open) -> WD-009 -> WD-010 -> WD-011 -> WD-012 -> WD-013 -> WD-014 -> WD-022b (M-Anthropic) -> WD-015. WD-022a is complete (see [DONE.md](DONE.md)). WD-016 is optional; WD-019 remains in the final milestone. M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

## M1 - Collection


- [ ] **WD-024 - Lightweight Rust adapter.** Implement a small native adapter before WD-009 while retaining the Python core and existing inbox contract. The user selected this fallback after WD-008 measured Python import/startup overhead above target. Establish the Rust build toolchain, preserve pause/allowlist/redaction/quota/loss semantics, and avoid a new agent framework. Acceptance: pytest-driven cross-language contract tests, compatible locks and atomic publication, installed-binary tests, and p95 hook wall time of 250 ms or less under the documented Windows workload. Complete the remaining WD-008 native matrix: concurrent CLI/desktop worktrees, restart persistence, desktop prompt submission, tool failure, subagent lifecycle, interrupt, compaction, and session end. The user moved these checks here after the Python adapter timed out in native Codex. Preserve earlier observations and report unavailable event triggers explicitly. macOS/Linux host validation remains WD-019. Recovery status (2026-09-06): 172 offline tests pass; direct installed Rust p95 is 111/226 ms, but shell p95 is 339/833 ms (Windows PowerShell) and 723/1375 ms (PowerShell 7), with a 2147 ms first PowerShell 7 call. Remaining gates: missing concurrent native PostToolUse callbacks, actual desktop prompt submission and desktop interrupt/compaction, and end-to-end latency. CLI interrupt/compaction/session end and live restart persistence are verified; see [WD-024 verification](docs/verification.md#wd-024-rust-adapter-acceptance-remains-open).

## M2 - Analysis

- [ ] **WD-009 - Codex transcript enrichment.** Versioned readers, offsets/rotation/partial tails, and usage reconciliation. Depends on WD-008/024. Acceptance: no cumulative usage double counting, unsupported readers do not interfere with hooks, and explicit cached/input/output availability.
- [ ] **WD-010 - Shadow findings and reports.** Fingerprints, comparable pytest/JUnit results, Git diff debounce, durations/output/compaction/usage, evidence IDs, and rule versions. Depends on WD-009. Acceptance: exit 0 does not imply progress, waiting does not imply a stall, and concurrent edits have uncertain attribution.
- [ ] **WD-011 - Labels, pin, and manual export.** Task outcome/type, Markdown/JSONL/manifest/prompt, session selection, and content review. Depends on WD-010. Acceptance: offline export without LLM/network, label round trips, purge of watchdog data only, and version/gaps in the manifest.
- [ ] **WD-012 - Calibration on real work.** Manually label a target of 20-50 sessions, measure precision/false positives/overhead, and identify typical tasks. Depends on WD-011. Acceptance: an auditable report with counts, limitations, and recommendations, without automatic harness changes.

## Later milestones: research gates

- [ ] **WD-013 - M3 guidance policy.** After WD-012, select calibrated rules for Codex coding sessions, cooldown/expiry, and provider-specific safe delivery. Acceptance: observe by default, opt-in, kill switch, and at least 90% precision with sample size disclosed; distinguish delivery from advice acceptance.
- [ ] **WD-014 - M4 LLM/control design.** After M3, decide CLI/API execution for Codex coding sessions, budget, isolation, and human-gate semantics. Acceptance: a separately accepted decision before implementation; checks against recursion and unintended data sharing.

## M-Anthropic - After WD-014, before WD-015

- [ ] **WD-022b - Claude enrichment, lifecycle, and guidance/control.** Scheduled after WD-014; builds on WD-022a. Add Claude transcript enrichment (versioned readers, offsets/rotation/partial tails), usage reconciliation, the wider content-capture surface (`error`, `duration_ms`, `is_interrupt`), remaining CLI/desktop lifecycle checks beyond observation, supported guidance/control delivery, and — separately recorded — async hook delivery only after the synchronous baseline is fixed and recorded. Acceptance: no cumulative usage double counting, mixed-provider isolation and bounded overhead, documented unavailable control capabilities, and durability of any backgrounded hook verified with the WD-022a evidence standard. No ordinary chats or Cowork. Does not block M1-M4; macOS/Linux validation remains WD-019.

## Optional extensions after M-Anthropic

- [ ] **WD-015 - App Server spike.** Scheduled after WD-022; not a dependency of M1-M4. Check ownership/attach to existing desktop/CLI sessions and available steer/interrupt operations. Acceptance: live evidence or an explicit limitation; add an adapter only if its benefit is confirmed.
- [ ] **WD-016 - Cross-project overview.** After a useful M2 release, evaluate a cross-project read-only view without merging databases and determine whether a web UI is needed.

## M5 - Deferred cross-platform validation

- [ ] **WD-019 - macOS/Linux host access and compatibility verification.** Obtain test hosts in the final milestone; verify package installation, hooks, detached launch, locks, path handling, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python core and existing probes. Acceptance: per-OS/version evidence and fixes for demonstrated differences, with unavailable surfaces explicitly marked. This task does not block WD-002, WD-008, M0-M4, or M-Anthropic; existing CI remains early feedback.
