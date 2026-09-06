# TODO

Queue: WD-009 -> WD-010 -> WD-011 -> WD-012 -> WD-013 -> WD-014 -> WD-022b (M-Anthropic) -> WD-015 -> WD-019 -> WD-026. WD-022a, WD-024, and WD-027 are complete (see [DONE.md](DONE.md)). WD-016 is optional; WD-026 is the final deferred provider-performance task. M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

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

- [ ] **WD-026 - Resolve Codex hook performance on Windows.** Deferred until the end of the queue after WD-019. Investigate the external Windows/Codex command-runner cost that remains after the Rust adapter and shared-resolution optimization. The observed standalone CLI PowerShell contract is p95 279 ms sequential / 563 ms four-way; direct Rust is 65 / 173 ms, while a nested CMD `commandWindows` variation fails before handler start. Acceptance: either a reproducible vendor-supported launch path that meets the documented latency target under the actual Codex runner, or an evidence-backed external limitation/issue report with versioned measurements and a documented operational decision. Do not relax the historical WD-024 result retroactively.
