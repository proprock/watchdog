# TODO

Queue: WD-002 -> WD-003 -> WD-004 -> WD-005 -> WD-006 -> WD-007 -> WD-008 -> WD-009 -> WD-010. Design: ROADMAP.md and docs/architecture.md. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

## M1 - Collection

- [ ] **WD-002 - Provider compatibility spike (closeout in progress).** Live Windows event evidence for both CLIs and both desktop paths is recorded in [the compatibility report](docs/provider-compatibility.md). Capture probe and eight offline tests pass. Remaining acceptance decision: retain explicit desktop event gaps and verify Codex/desktop detached-core survival under WD-005/WD-008, or complete those experiments here. macOS/Linux validation remains M5 / WD-019. Native Codex trust was approved through the product; no bypass was used.
- [ ] **WD-003 - Config, registry, envelope.** Project UUIDs, Git common-dir/worktrees, non-Git roots, relocation, typed events, and schema version; add ty. Depends on WD-002. Acceptance: isolation, path casing/symlink fixtures, explicit unknown IDs, and configuration errors.
- [ ] **WD-004 - Inbox and project storage.** Atomic writes, SQLite migrations/WAL, transaction/replay, single writer, bounded quarantine, and artifacts. Depends on WD-003. Acceptance: crash before/after commit and replay without duplicates; malformed events do not block processing; newer schemas are not overwritten.
- [ ] **WD-005 - Daemon lifecycle.** OS lock, detached start, bounded polling, pause/start/stop/status, and control requests. Depends on WD-004. Acceptance: concurrent startup, stale heartbeat/PID reuse, harness exit (including Codex and both desktop process containers, which WD-002 did not verify), crash recovery, and pause; tests clean up only their own processes.
- [ ] **WD-006 - Hook adapters and installer.** Codex/Claude, provider-specific no-op responses/fail-open, project allowlist, dry-run/backup/idempotent install/uninstall without damaging existing hooks. Depends on WD-005. Acceptance: real schemas/fixtures, paths with spaces, Windows shell quoting, timeout/missing daemon, and native trust/reload checks. Use the WD-002 observed shapes without assuming a universal tool result or exit-code field.
- [ ] **WD-007 - Retention, quotas, redaction.** 30/180 days, 2 GiB/project, 64 MiB inbox, 1 MiB payload, configurable overrides/pin/reserve/loss counters. Depends on WD-004/006. Acceptance: quotas during concurrent ingestion, WAL/cleanup, pinned saturation, disk full, and known secrets absent from persistent output. Vendor transcripts are never deleted.
- [ ] **WD-008 - CLI observation and M1 validation.** Project commands, doctor, sessions list/show; latency/idle overhead baseline. Depends on WD-005/006/007. Acceptance: two providers in worktrees on Windows, data surviving restart, explicit gaps, and a verified matrix including remaining WD-002 desktop event gaps (tool failure, interrupt, and per-surface compaction); measured p95 hook latency target of 250 ms or less. Other-OS compatibility acceptance is deferred to M5 / WD-019.

## M2 - Analysis

- [ ] **WD-009 - Transcript enrichment.** Versioned readers, offsets/rotation/partial tails, and usage reconciliation. Depends on WD-008. Acceptance: no cumulative usage double counting, unsupported readers do not interfere with hooks, and explicit cached/input/output availability.
- [ ] **WD-010 - Shadow findings and reports.** Fingerprints, comparable pytest/JUnit results, Git diff debounce, durations/output/compaction/usage, evidence IDs, and rule versions. Depends on WD-009. Acceptance: exit 0 does not imply progress, waiting does not imply a stall, and concurrent edits have uncertain attribution.
- [ ] **WD-011 - Labels, pin, and manual export.** Task outcome/type, Markdown/JSONL/manifest/prompt, session selection, and content review. Depends on WD-010. Acceptance: offline export without LLM/network, label round trips, purge of watchdog data only, and version/gaps in the manifest.
- [ ] **WD-012 - Calibration on real work.** Manually label a target of 20-50 sessions, measure precision/false positives/overhead, and identify typical tasks. Depends on WD-011. Acceptance: an auditable report with counts, limitations, and recommendations, without automatic harness changes.

## Later milestones: research gates

- [ ] **WD-013 - M3 guidance policy.** After WD-012, select calibrated rules, cooldown/expiry, and provider-specific safe delivery. Acceptance: observe by default, opt-in, kill switch, and at least 90% precision with sample size disclosed; distinguish delivery from advice acceptance.
- [ ] **WD-014 - M4 LLM/control design.** After M3, decide CLI/API execution, budget, isolation, and human-gate semantics. Acceptance: a separately accepted decision before implementation; checks against recursion and unintended data sharing.
- [ ] **WD-015 - App Server spike.** Check ownership/attach to existing desktop/CLI sessions and available steer/interrupt operations. Acceptance: live evidence or an explicit limitation; add an adapter only if its benefit is confirmed.
- [ ] **WD-016 - Cross-project overview.** After a useful M2 release, evaluate a cross-project read-only view without merging databases and determine whether a web UI is needed.

## M5 - Deferred cross-platform validation

- [ ] **WD-019 - macOS/Linux host access and compatibility verification.** Obtain test hosts in the final milestone; verify package installation, hooks, detached launch, locks, path handling, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python core and existing probes. Acceptance: per-OS/version evidence and fixes for demonstrated differences, with unavailable surfaces explicitly marked. This task does not block WD-002, WD-008, or M0-M4; existing CI remains early feedback.
