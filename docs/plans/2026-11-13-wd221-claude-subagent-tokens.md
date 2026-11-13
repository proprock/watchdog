# WD-221 Claude subagent token investigation

**Goal:** Explain the WD-195 Claude subagent differences from source records,
add synthetic regression coverage, and repeat the comparison.

**Decision:** Compare immutable historical receipts with first and final raw
response blocks before considering any accounting change. Do not rewrite raw
records or historical events to manufacture parity. No model calls, provider
probes, schema changes, or live installation changes are needed.

1. Read the saved WD-195 snapshot and match each affected stream's native
   request identity to captured raw records. Account for every token dimension.
2. Replay the same captured streams into fresh temporary collector stores and
   compare final usage against the saved Insights projection. Record unchanged
   historical comparisons separately from replay results.
3. Add synthetic coverage for simultaneous growth of output and cache usage
   in subagent response fragments, including an incremental import. Existing
   behavior should pass; no production change or red/green pair is warranted
   unless a current defect is reproduced.
4. Run focused tests, both full suites, and applicable static checks. Review the
   diff and evidence, then move WD-221 from TODO.md to DONE.md only when all
   requested deliverables are verified.

**Files:** `tests/test_claude_transcripts.py`,
`insights/tests/test_ingest_claude.py`, `docs/evidence/wd221.md`, `TODO.md`,
`DONE.md`. Detailed real-session artifacts stay under ignored `tests/local/`.
