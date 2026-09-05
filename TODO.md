# TODO

Queue: WD-006 -> WD-007 -> WD-008 -> WD-009 -> WD-010 -> WD-011 -> WD-012 -> WD-013 -> WD-014 -> WD-022 (M-Anthropic) -> WD-015. WD-016 is optional; WD-019 remains in the final milestone. M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

## M1 - Collection

- [ ] **WD-006 - Codex hook adapter and installer.** Codex CLI/local desktop coding, provider-specific no-op responses/fail-open, project allowlist, dry-run/backup/idempotent install/uninstall without damaging existing hooks. Depends on WD-005. Acceptance: real schemas/fixtures, paths with spaces, Windows shell quoting, timeout/missing daemon, and native trust/reload checks. Use the WD-002 observed shapes without assuming a universal tool result or exit-code field.
- [ ] **WD-007 - Retention, quotas, redaction.** 30/180 days, 2 GiB/project, 64 MiB inbox, 1 MiB payload, configurable overrides/pin/reserve/persistent loss counters, and cleanup of stale temporary files and unreferenced artifacts left by interrupted writes. Depends on WD-004/006. Acceptance: quotas during concurrent ingestion, WAL/cleanup, pinned saturation, disk full, and known secrets absent from persistent output. Vendor transcripts are never deleted.
- [ ] **WD-008 - CLI observation and M1 validation.** Project commands, doctor, sessions list/show; latency/idle overhead baseline. Depends on WD-005/006/007. Acceptance: multiple concurrent Codex sessions in worktrees on Windows, data surviving restart, explicit gaps, and a verified matrix including remaining WD-002 Codex desktop event gaps (prompt submission, tool failure, subagent lifecycle, interrupt, compaction, and session end); measured p95 hook latency target of 250 ms or less. Other-OS compatibility acceptance is deferred to M5 / WD-019.

## M2 - Analysis

- [ ] **WD-009 - Codex transcript enrichment.** Versioned readers, offsets/rotation/partial tails, and usage reconciliation. Depends on WD-008. Acceptance: no cumulative usage double counting, unsupported readers do not interfere with hooks, and explicit cached/input/output availability.
- [ ] **WD-010 - Shadow findings and reports.** Fingerprints, comparable pytest/JUnit results, Git diff debounce, durations/output/compaction/usage, evidence IDs, and rule versions. Depends on WD-009. Acceptance: exit 0 does not imply progress, waiting does not imply a stall, and concurrent edits have uncertain attribution.
- [ ] **WD-011 - Labels, pin, and manual export.** Task outcome/type, Markdown/JSONL/manifest/prompt, session selection, and content review. Depends on WD-010. Acceptance: offline export without LLM/network, label round trips, purge of watchdog data only, and version/gaps in the manifest.
- [ ] **WD-012 - Calibration on real work.** Manually label a target of 20-50 sessions, measure precision/false positives/overhead, and identify typical tasks. Depends on WD-011. Acceptance: an auditable report with counts, limitations, and recommendations, without automatic harness changes.

## Later milestones: research gates

- [ ] **WD-013 - M3 guidance policy.** After WD-012, select calibrated rules for Codex coding sessions, cooldown/expiry, and provider-specific safe delivery. Acceptance: observe by default, opt-in, kill switch, and at least 90% precision with sample size disclosed; distinguish delivery from advice acceptance.
- [ ] **WD-014 - M4 LLM/control design.** After M3, decide CLI/API execution for Codex coding sessions, budget, isolation, and human-gate semantics. Acceptance: a separately accepted decision before implementation; checks against recursion and unintended data sharing.

## M-Anthropic - After WD-014, before WD-015

- [ ] **WD-022 - Independent Claude coding support.** Scheduled after WD-014; reuse M1-M4 contracts and preserve existing spike evidence. Add Claude Code CLI/local desktop Code adapters, installer/uninstaller, transcript enrichment, and supported guidance/control delivery. Acceptance: refresh schemas/trust, complete remaining Windows Claude event checks, apply WD-005 lifecycle checks to actual CLI/desktop exit, verify mixed-provider isolation and bounded overhead, and document unavailable control capabilities. No ordinary chats or Cowork. Does not block M1-M4; macOS/Linux validation remains WD-019.

## Optional extensions after M-Anthropic

- [ ] **WD-015 - App Server spike.** Scheduled after WD-022; not a dependency of M1-M4. Check ownership/attach to existing desktop/CLI sessions and available steer/interrupt operations. Acceptance: live evidence or an explicit limitation; add an adapter only if its benefit is confirmed.
- [ ] **WD-016 - Cross-project overview.** After a useful M2 release, evaluate a cross-project read-only view without merging databases and determine whether a web UI is needed.

## M5 - Deferred cross-platform validation

- [ ] **WD-019 - macOS/Linux host access and compatibility verification.** Obtain test hosts in the final milestone; verify package installation, hooks, detached launch, locks, path handling, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python core and existing probes. Acceptance: per-OS/version evidence and fixes for demonstrated differences, with unavailable surfaces explicitly marked. This task does not block WD-002, WD-008, M0-M4, or M-Anthropic; existing CI remains early feedback.
