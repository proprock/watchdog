# WD-024 remaining blockers — per-provider attribution

Checkpoints: WD-024 at `87f79b0`; WD-022a on `feature/wd-022a-claude-observation`
(**gate met, closing**). Rust adapter contracts and the full offline suite pass.
Direct installed-binary p95 is 111 ms sequential / 226 ms four callers (Codex
generated command) and, after WD-022a removed the `git rev-parse` subprocess from
resolution, **36 ms / 96 ms** (Claude exec form, `scripts/benchmark_hooks.py`;
was 82 / 153). Measured evidence:
[WD-024](docs/verification.md#wd-024-rust-adapter-acceptance-remains-open) and
[WD-022a](docs/verification.md#wd-022a-claude-observation-gate-met).

## Blocker × provider × attributed cause

| Blocker | Codex (WD-024) | Claude (WD-022a) | Attributed cause |
|---|---|---|---|
| **Native launch latency** | Windows PowerShell p95 339/833 ms; PowerShell 7 p95 723/1375 ms; first pwsh call 2147 ms vs Codex's fixed 2 s deadline. Direct native launch not available through Codex. | Exec form (no shell) four-caller p95 **96 ms**, max 105 ms, synthetic (was 153/199 before the git-subprocess removal). Interactive `hookInfos.durationMs` present for `Stop` only: 51–71 ms. | **Shell startup + a per-hook `git rev-parse` spawn**, not core adapter work. Removing the shell (Claude exec form) *and* the git subprocess (WD-022a Part 2.2, shared binary) brings launch well within budget. Codex has no exec form, so its shell cost stands; it does inherit the git-subprocess removal. |
| **Missing / dropped callbacks under concurrency** | Concurrent run: PostToolUse absent for tool calls that demonstrably completed. Zero project loss counters could not distinguish "never ran" from "killed before publish". | First pass: one `SessionStart` dropped (counted `invalid` loss). **After removing the git subprocess: four concurrent `claude -p` sessions each stored a complete 8-event set, all loss counters zero across a daemon restart, no `faults.log`.** The pre-fix defect did not reproduce. `disk::fault` now records an attributable, content-free reason per failure. | The dropped `SessionStart` was **adapter-side contention on the per-hook `git rev-parse` spawn** during concurrent cold starts. Removing that spawn resolved it on Claude. Codex's missing PostToolUse is *plausibly* the same root cause (shared path) but was never instrumented to confirm; the `disk::fault` evidence now makes any recurrence attributable. |
| **Desktop event coverage** | Task-API follow-ups produced no UserPromptSubmit; real composer submission and desktop interrupt/compaction never exercised. | **Done for Claude (WD-022a Stage 4).** Supervised desktop pass: real composer submission → stored `turn.start`; SubagentStart/Stop, `/compact`, `PostToolUseFailure`, `SessionEnd` (on archive) all stored; Esc produced no event (`Interrupt` absent) but the tool call still completed. 38 events, zero losses. | Claude desktop path is covered. Codex desktop remains uncovered; desktop behaviour is not inferable from CLI results, and Claude's is not inferable for Codex. |

## What the Claude evidence proves — and does not — about Codex

**Proves:**
- The shared Rust binary's own launch cost is low when no shell wraps it and the
  per-hook `git rev-parse` spawn is gone: 96 ms four-caller p95, 36 ms
  sequential. Both changes are in the shared binary, so Codex inherits the
  git-subprocess removal.
- The dropped-observation-under-concurrency blocker was, on Claude, adapter-side
  contention on that git subprocess: removing it made the defect stop
  reproducing across a four-concurrent Pass A with zero losses.
- The empty-stdout no-op, `timeout: 2`, exec form, `PostToolUseFailure` →
  `tool.finish`, restart preservation, and full desktop lifecycle (composer
  submission, subagent, `/compact`, `SessionEnd`) all work against real Claude
  CLI and desktop.

**Does not prove:**
- Any Codex end-to-end p95. Codex enforces a fixed 2 s deadline, runs through a
  shell, and gives no harness-side timing; the Claude numbers do not transfer.
- That Codex's missing PostToolUse had the *same* root cause. It is plausibly the
  shared git-subprocess contention (now removed), but Codex was never
  instrumented to confirm — re-run the Codex concurrent matrix to check.
- Codex desktop coverage.

## Next steps (user's decision)

1. **WD-022a is closing** — the concurrent `invalid` loss is fixed (git
   subprocess removed) with `disk::fault` attribution, and the gate is met.
2. Re-run the Codex concurrent-delivery matrix against the fixed binary: the
   git-subprocess removal may also clear WD-024 blocker #2 for Codex.
3. Decide whether to close WD-024, redefine it, or split the Codex shell-launch
   problem (blocker #1, unchanged) into its own task.
4. When both WD-024 and WD-022a/b are settled, delete the `tmp-WD-0*.md` working
   files.
