# TODO

Queue: WD-108 -> WD-012 -> WD-101/WD-102 -> WD-013 -> WD-014 -> WD-022b (M-Anthropic) -> WD-015 -> WD-019 -> WD-026. WD-009, WD-010, WD-011, WD-022a, WD-024, WD-027, WD-103, and WD-107 are complete (see [DONE.md](DONE.md)). WD-016 is optional; WD-026 is the final deferred provider-performance task. M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. The [WD-103 audit](docs/software-factory-audit.md) records the evidence boundary and follow-up priorities. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

## M2 - Analysis

- [ ] **WD-108 - Safe transcript-enrichment failure diagnostics.** Classify every contained transcript-enrichment failure in the daemon log with a fixed, allowlisted `error_type`, so a degraded project can be diagnosed without opening private transcript content. Preserve the existing privacy boundary: never log transcript paths, raw records, prompts, tool input/output, exception messages, or tracebacks. Acceptance: deterministic tests force each handled exception category and prove the status remains degraded while the log contains only safe component/event/decision/project identity/error-type metadata; successful collection and unknown-field warnings remain unaffected. No live provider probe is authorized by this task alone. WD-012 depends on this diagnostic improvement.

- [ ] **WD-012 - Calibration on real work.** Before calibration, extend the provider-scoped session labels with `progress`, `slow`, `stuck`, and `externally_blocked`, plus a per-finding verdict of `true_positive`, `false_positive`, or `uncertain`. Then manually label a target of 20-50 real sessions, including sessions with no emitted finding so false negatives can be counted. Measure finding precision, false positives, false negatives, overhead, and the distribution of task/session states. Depends on WD-011 and is the evidence gate for WD-013. Acceptance: an auditable report with the dataset definition, counts, confusion table, limitations, and recommendations, without automatic harness changes.

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

## Audit follow-ups

- [ ] **WD-101 - Test realism and boundary coverage.** Replace the exact command-list assertion in the CLI help test with a subprocess check that also proves an isolated `--home` remains untouched; fold the self-referential Claude fixture completeness check into the behavioral mapping test or tie it to an independently captured contract. Add a subprocess smoke for project registration, daemon lifecycle, and report; a Python hook -> daemon -> SQLite path in both content modes; and harmless argument round-trips through each supported shell available on the host. Keep mocked polling and synthetic stream tests as focused units, but do not present them as live evidence. Acceptance: focused and full offline suites pass, each new test states its evidence class, all owned processes/state are cleaned up, and no provider is invoked.
- [ ] **WD-102 - Manual and live evidence gates.** Define a versioned result record that separately reports provider dispatch, adapter start/failure, missing callback, retry count, persisted loss category, end-to-end delivery, and cleanup. Apply it to future isolated provider probes and retain provider/harness/OS versions plus sanitized provenance. Acceptance: synthetic, local adapter, and live-provider results cannot be conflated; every nonzero failure/loss is attributable or explicitly unknown; owned hooks, processes, and temporary state have recorded cleanup. No live probe is authorized by this backlog entry alone.

## Misc

- [] **WD-103 - manual/local gates in adapter (retries count, failures, etc.)** 
