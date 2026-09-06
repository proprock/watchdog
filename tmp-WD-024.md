# WD-024 remaining blockers — per-provider attribution

Checkpoints: WD-024 at `87f79b0`; WD-022a on `feature/wd-022a-claude-observation`.
Rust adapter contracts and the full offline suite pass. Direct installed-binary
p95 is 111 ms sequential / 226 ms four callers (Codex generated command) and
82 ms / 153 ms (Claude exec form, `scripts/benchmark_hooks.py`). Measured
evidence: [WD-024](docs/verification.md#wd-024-rust-adapter-acceptance-remains-open)
and [WD-022a](docs/verification.md#wd-022a-claude-observation-gate-not-met).

## Blocker × provider × attributed cause

| Blocker | Codex (WD-024) | Claude (WD-022a) | Attributed cause |
|---|---|---|---|
| **Native launch latency** | Windows PowerShell p95 339/833 ms; PowerShell 7 p95 723/1375 ms; first pwsh call 2147 ms vs Codex's fixed 2 s deadline. Direct native launch not available through Codex. | Exec form (no shell) four-caller p95 **153 ms**, max 199 ms, synthetic. `bash -c` p95 240 ms. Harness-side `durationMs` **not exposed** by Claude Code 2.1.259 for `claude -p`; the `hook_started`/`hook_response` stream delta is unreliable (sequential p95 1906 ms > concurrent 515 ms). | **Shell startup**, not adapter work. Removing the shell (Claude exec form) brings launch within budget. Codex has no exec form, so its shell cost stands. The end-to-end harness-blocking number is still unmeasured on both. |
| **Missing / dropped callbacks under concurrency** | Concurrent run: PostToolUse absent for tool calls that demonstrably completed. Zero project loss counters could not distinguish "never ran" from "killed before publish". | Four concurrent `claude -p` sessions: **one `SessionStart` dropped**, adapter `invalid` loss counter incremented (fail-open, counted); a sequential probe produced further `invalid` losses. All 8 `tool_use_id`s had a matching stored `tool.finish`. | **Adapter-side transient failure** in the git / config / registry path under concurrent session starts, now *observed and counted* on Claude where Codex could only be suspected. Needs runner start/exit/timeout evidence per dropped event. |
| **Desktop event coverage** | Task-API follow-ups produced no UserPromptSubmit; real composer submission and desktop interrupt/compaction never exercised. | Not run. The supervised desktop pass (real composer submission → stored `turn.start`, one subagent, `/compact`, Escape interrupt, session unload) is pending. | Unchanged. Desktop behaviour is not inferable from CLI results on either provider. |

## What the Claude evidence proves — and does not — about Codex

**Proves:**
- The shared Rust binary's own launch cost is low when no shell wraps it
  (153 ms four-caller p95). The earlier 339–1375 ms Codex figures are
  attributable to shell startup, not to the adapter.
- Concurrent session starts can make the adapter drop an observation. On Claude
  this is now a counted `invalid` loss with a reproduction, confirming the
  "missing callbacks" blocker is at least partly an adapter concurrency defect
  and not solely a harness artefact.
- The empty-stdout no-op, `timeout: 2`, exec form, `PostToolUseFailure` →
  `tool.finish`, and restart preservation all work against a real Claude harness.

**Does not prove:**
- Any Codex end-to-end p95. Codex enforces a fixed 2 s deadline, runs through a
  shell, and gives no harness-side timing; the Claude numbers do not transfer.
- That Codex's missing PostToolUse has the *same* root cause as Claude's dropped
  `SessionStart`. Both point at the adapter's concurrent git/config path, but
  Codex was never instrumented to confirm it.
- Desktop coverage for either provider.

## Next steps (user's decision)

1. Fix the concurrent `invalid` loss in the adapter's resolve/admission path;
   add per-invocation runner start/exit/timeout evidence so a dropped event is
   attributable.
2. Decide the timing source for the WD-022a gate now that `durationMs` is absent
   from `claude -p`: an interactive-session transcript (where WD-002 saw
   `durationMs`), a corrected stream-timestamp method, or a redefined gate.
3. Run WD-022a Pass B (co-resident user hooks), Pass C (shell attribution on a
   live Claude session), and the supervised desktop pass.
4. Decide whether to close WD-024, redefine it, or split the Codex shell-launch
   problem into its own task.
