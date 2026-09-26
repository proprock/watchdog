# TODO

Open-work order: WD-015 next. WD-016 is optional after a useful M2 release. Deferred validation/performance work is WD-019 -> WD-026; post-MVP release engineering (WD-109, WD-110) is complete; WD-117 is an optional local real-transcript corpus. WD-009, WD-010, WD-011, WD-012, WD-013, WD-014, WD-022a, WD-022b, WD-024, WD-027, WD-101, WD-102, WD-103, WD-107, WD-108, WD-109, WD-110, WD-111, WD-112, WD-113, WD-114, WD-115, WD-116, WD-118, and WD-119 are complete (see [DONE.md](DONE.md)). M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. The [WD-103 audit](docs/software-factory-audit.md) records the evidence boundary and follow-up priorities. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

A new, documented capability gap surfaced during WD-022b's live verification, not yet its own task: on both Claude CLI and Desktop, a fan-out subagent spawn (many children from one request) reliably undercounts `SubagentStart` relative to `SubagentStop` (zero adapter losses; a Claude Code dispatch limitation, not Watchdog's) -- see [verification.md](docs/verification.md#wd-022b-live-verification-trustreload-capability-gaps-process-lifecycle). Deployment of the WD-022b changes to the user's live Codex-hook instance (`LIVE.md`) is a separate, pending decision.

## Optional extensions after WD-022b

- [ ] **WD-015 - App Server spike.** Scheduled after WD-022; not a dependency of M1-M4. Check ownership/attach to existing desktop/CLI sessions and available steer/interrupt operations. Acceptance: live evidence or an explicit limitation; add an adapter only if its benefit is confirmed.
- [ ] **WD-016 - Cross-project overview.** After a useful M2 release, evaluate a cross-project read-only view without merging databases and determine whether a web UI is needed.

## M5 - Deferred cross-platform validation

- [ ] **WD-019 - macOS/Linux host access and compatibility verification.** Obtain test hosts in the final milestone; verify package installation, hooks, detached launch, locks, path handling, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python core and existing probes. Acceptance: per-OS/version evidence and fixes for demonstrated differences, with unavailable surfaces explicitly marked. This task does not block WD-002, WD-008, M0-M4, or M-Anthropic; existing CI remains early feedback.

- [ ] **WD-026 - Resolve Codex hook performance on Windows.** Deferred until the end of the queue after WD-019. Investigate the external Windows/Codex command-runner cost that remains after the Rust adapter and shared-resolution optimization. The observed standalone CLI PowerShell contract is p95 279 ms sequential / 563 ms four-way; direct Rust is 65 / 173 ms, while a nested CMD `commandWindows` variation fails before handler start. Acceptance: either a reproducible vendor-supported launch path that meets the documented latency target under the actual Codex runner, or an evidence-backed external limitation/issue report with versioned measurements and a documented operational decision. Do not relax the historical WD-024 result retroactively.

## Privacy model correction

Watchdog observes one user's own coding sessions, whose prompts and outputs have already been sent to the provider, and analyzes them locally without transmitting anything off the machine without the user's sanction. Decisions that rank privacy above local fidelity and usefulness are wrong for this threat model. The decision text is already updated in [docs/architecture.md](docs/architecture.md) ("Local fidelity and redaction"); these tasks bring the implementation in line.

- [ ] **WD-117 (optional) - Local real-transcript corpus.** Wire a `tests/local/` path (git-ignored, added to `.gitignore`) that regression and evaluation suites discover for real transcripts of the user's own sessions, skipping cleanly when the directory is absent. Add a `conftest.py` fixture that enumerates the corpus and a documented layout note. Keep committed fixtures synthetic. Acceptance: with no `tests/local/`, the full offline suite is unchanged; with a sample transcript present, the corpus tests run against it; Ruff and ty pass. TENETS and `tests/fixtures/hooks/README.md` are already updated.
