# TODO

Open-work order: WD-015 next; the `insights` scope extensions WD-132 -> WD-133 -> WD-134 are independent of it, and WD-132 is the first concrete step of WD-016's cross-project view. WD-016 is optional after a useful M2 release. The implementation audit leaves WD-120 and WD-121 as follow-up work; WD-122 is complete, but the M3 guidance gate remains closed. Deferred validation/performance work is WD-019 -> WD-026; post-MVP release engineering (WD-109, WD-110) is complete; WD-117 is an optional local real-transcript corpus. WD-009, WD-010, WD-011, WD-012, WD-013, WD-014, WD-022a, WD-022b, WD-024, WD-027, WD-101, WD-102, WD-103, WD-107, WD-108, WD-109, WD-110, WD-111, WD-112, WD-113, WD-114, WD-115, WD-116, WD-118, WD-119, WD-122, WD-123, WD-124, WD-125, WD-126, WD-127, WD-128, WD-129, WD-130, and WD-131 are complete (see [DONE.md](DONE.md)). M1-M4 support Codex coding only; ordinary chats are excluded. Design: ROADMAP.md and docs/architecture.md. The [WD-103 audit](docs/software-factory-audit.md) records the evidence boundary and follow-up priorities. This file contains open work only; completed entries move to [DONE.md](DONE.md) with their IDs and verification evidence. Pending work has not yet been verified.

A new, documented capability gap surfaced during WD-022b's live verification, not yet its own task: on both Claude CLI and Desktop, a fan-out subagent spawn (many children from one request) reliably undercounts `SubagentStart` relative to `SubagentStop` (zero adapter losses; a Claude Code dispatch limitation, not Watchdog's) -- see [verification.md](docs/verification.md#wd-022b-live-verification-trustreload-capability-gaps-process-lifecycle). Deployment of the WD-022b changes to the user's live Codex-hook instance (`LIVE.md`) is a separate, pending decision.

## Optional extensions after WD-022b

- [ ] **WD-015 - App Server spike.** Scheduled after WD-022; not a dependency of M1-M4. Check ownership/attach to existing desktop/CLI sessions and available steer/interrupt operations. Acceptance: live evidence or an explicit limitation; add an adapter only if its benefit is confirmed.
- [ ] **WD-016 - Cross-project overview.** After a useful M2 release, evaluate a cross-project read-only view without merging databases and determine whether a web UI is needed.

## LLM-assisted insights beyond one project (after WD-126)

WD-123 to WD-126 shipped seven `insights` modes over one project, plus the single-session judge ([docs/cli.md](docs/cli.md#insights)). The next steps widen the scope while the bundle stays aggregated, never raw content. Measured Sonnet 5 list cost through `claude -p` is about 4.2 USD per million bundle tokens (1-hour cache write plus the structured-output cache read), plus 10 USD per million answer tokens, usually 10-30K. A full 800K bundle is about 3.6 USD, and all seven modes on one project about 3.8 USD. On a subscription these calls consume usage limits rather than billing, so every step stays an explicit user command.

- [ ] **WD-132 - Cross-project insights for user-level rules.** Add `--all-projects` to `insights errors`, `permissions`, and `workflow`, then to the other project-wide modes where it helps. The mode builders read each registered project's database through its own read-only snapshot, never a merged store (the WD-016 boundary). They skip any project with `insights_llm_enabled = false` and record it in coverage.
  - **Merging.** Error clusters, permission classes, and workflow sequences merge across projects by signature or class. Every merged item carries the projects it occurred in and the counts per project, and it ranks by the number of projects first, then by frequency.
  - **Answer.** Each recommendation gains a `scope` of `user` or `project`, with a target file: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, or the user `~/.claude/settings.json` for user scope, and the project's own files for project scope. A pattern seen in only one project is never proposed at user scope.
  - **Acceptance:**
    - offline tests with two synthetic projects: a shared pattern merges with both projects listed, a one-project pattern stays project-scoped, and a disabled project is excluded and counted;
    - the fitted bundle stays within budget with nothing raw;
    - one live run over the registered projects recorded in verification, with cost and unchanged session counts.
- [ ] **WD-133 - `insights sessions`: project session triage.** A project-wide mode over the window's sessions.
  - **Bundle.** A deterministic, compact facet per session, about 1-2K tokens: the first prompt excerpt, turns, calls, failures, loops, measured tokens, compactions, subagents, shadow findings, whether it ended, the last assistant message excerpt, and any recorded label. Sessions are ranked by signals of trouble (failures, loops, findings, no end), so a budget cut drops the calmest sessions.
  - **Answer.** One model call returns:
    - candidate `stuck`/`blocked`/abandoned sessions, each with evidence and a suggestion to open it with `insights session`;
    - recurring session-level patterns, such as unrelated tasks mixed in one session, sessions abandoned after the same kind of failure, or long sessions that never compact;
    - optional `log`-only rule candidates.

    It never labels a session or stores anything, and it makes no per-session model calls.
  - **Acceptance:**
    - offline tests for facet contents, trouble ranking, and budget cutting;
    - a fake-runner test for validation and grounding;
    - one live run on the `watchdog` project with cost recorded.
- [ ] **WD-134 - `insights digest`: one cross-mode synthesis.** Run the deterministic builders of `errors`, `context`, `tokens`, `workflow`, `subagents`, and `permissions` for one window. Each mode keeps its facts and coverage plus its top items within a declared share of the bundle budget. One model call then explains links between modes, such as an environment failure causing retry loops that inflate context, and ranks a short action list across them.
  - **Links.** A link must cite evidence from at least two modes; otherwise it is reported as a single-mode item.
  - **Relation to the modes.** Per-mode reports stay available; the digest does not replace them. It may later reuse WD-132's `--all-projects`.
  - **Acceptance:**
    - offline tests for per-mode budget shares, including a mode with no items;
    - grounding of cross-mode links against the ids of both modes;
    - one live run with cost, compared against the sum of the seven separate reports.

## Implementation audit follow-ups

- [ ] **WD-120 - End-to-end collection readiness.** Add a diagnostic distinct from `doctor`'s local storage/daemon health. Report separately whether the selected provider hook is installed and points to a usable native artifact, whether its trust/callback state can be observed, whether a controlled adapter event reaches the spool, and whether daemon admission reaches the selected project's SQLite store. A local synthetic probe must be explicitly labeled as such; a real provider callback requires a separate opt-in live probe with provider/version/surface provenance. Do not treat zero loss counters, a running daemon, or an unobservable trust state as proof of provider coverage. Acceptance: machine-readable `ready`/`failed`/`unknown` results for each stage with actionable evidence; offline tests for absent/broken hook, stopped daemon, unresolved project, and successful local-adapter delivery; no mutation of active provider settings or implicit provider invocation by the ordinary diagnostic.
- [ ] **WD-121 - Cover staged and untracked work in diff fingerprints.** Define a bounded, reproducible checkout-state fingerprint that includes unstaged tracked changes plus staged and untracked changes, with explicit treatment of ignored files, renames, binary content, and concurrent editors. Keep snapshots read-only: no index/worktree mutation, and no captured file content in the report. Preserve an `unknown` result on Git failure or a limit being exceeded rather than calling the checkout clean. Acceptance: real-Git tests show staged-only and untracked-only A->B->A transitions can produce evidence; a mixed-state test distinguishes checkout changes without false clean snapshots; documentation states the exact coverage and remaining attribution limits; latency and size bounds are measured against representative checkouts before adopting the new fingerprint.

## M5 - Deferred cross-platform validation

- [ ] **WD-019 - macOS/Linux host access and compatibility verification.** Obtain test hosts in the final milestone; verify package installation, hooks, detached launch, locks, path handling, and cleanup for both CLIs and officially available desktop surfaces. Reuse the Python core and existing probes. Acceptance: per-OS/version evidence and fixes for demonstrated differences, with unavailable surfaces explicitly marked. This task does not block WD-002, WD-008, M0-M4, or M-Anthropic; existing CI remains early feedback.

- [ ] **WD-026 - Resolve Codex hook performance on Windows.** Deferred until the end of the queue after WD-019. Investigate the external Windows/Codex command-runner cost that remains after the Rust adapter and shared-resolution optimization. The observed standalone CLI PowerShell contract is p95 279 ms sequential / 563 ms four-way; direct Rust is 65 / 173 ms, while a nested CMD `commandWindows` variation fails before handler start. Acceptance: either a reproducible vendor-supported launch path that meets the documented latency target under the actual Codex runner, or an evidence-backed external limitation/issue report with versioned measurements and a documented operational decision. Do not relax the historical WD-024 result retroactively.

## Privacy model correction

Watchdog observes one user's own coding sessions, whose prompts and outputs have already been sent to the provider, and analyzes them locally without transmitting anything off the machine without the user's sanction. Decisions that rank privacy above local fidelity and usefulness are wrong for this threat model. The decision text is already updated in [docs/architecture.md](docs/architecture.md) ("Local fidelity and redaction"); these tasks bring the implementation in line.

- [ ] **WD-117 (optional) - Local real-transcript corpus.** Wire a `tests/local/` path (git-ignored, added to `.gitignore`) that regression and evaluation suites discover for real transcripts of the user's own sessions, skipping cleanly when the directory is absent. Add a `conftest.py` fixture that enumerates the corpus and a documented layout note. Keep committed fixtures synthetic. Acceptance: with no `tests/local/`, the full offline suite is unchanged; with a sample transcript present, the corpus tests run against it; Ruff and ty pass. TENETS and `tests/fixtures/hooks/README.md` are already updated.
