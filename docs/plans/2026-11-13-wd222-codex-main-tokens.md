# WD-222 Codex main-stream investigation plan

**Goal:** Explain the 28 dimension/session differences across 12 Codex main streams recorded by WD-195, correct verified defects, and repeat the comparison without replacing unknown values with zero.

**Approach:** Open the original Insights snapshot read-only and compare it with its saved report. Inspect captured raw Codex records and source coverage before deciding whether the difference is a collector defect, an Insights defect, or historical/source behavior. Preserve raw records and historical receipts.

**Scope:** Codex main streams only; subagent accounting belongs to WD-223. No provider calls, new dependencies, schema changes, or speculative accounting changes.

1. Reproduce the historical comparison using `wdi.metrics.hooks.reconcile_tokens`; retain all unavailable dimensions.
2. In ignored `tests/local/wd222_reconcile.py`, correlate captured source streams, snapshot import positions, native usage receipts, and raw `token_usage_record` / `event_msg.token_count` records. Produce a local report with explicit coverage limits.
3. Replay affected streams through current parsers/collector into isolated stores. Explain the complete numerical difference, including values below the reporting threshold.
4. For any verified current defect, add a synthetic failing behavioral regression in the affected parser/collector test module, run it, commit the red test as required, apply the smallest fix, and verify green. If the behavior is already correct, add useful characterization coverage without an artificial red change.
5. Run focused affected tests, then `uv run pytest` and `uv run --directory insights pytest` once on the stable diff. Run applicable Ruff, ty, build, and diff checks. Request a bounded independent review after evidence is ready.
6. Record findings, provenance, limitations, and checks in `docs/evidence/wd222.md`. Move WD-222 from `TODO.md` to `DONE.md` only after all deliverables are met; record any deferred findings as separate open tickets.

Work remains in the current checkout on `feature/wd-222-codex-main-tokens`, per TENETS.md. Installation is needed only if a verified production observation/control change is made.
