# WD-223 Codex subagent investigation plan

**Goal:** Explain the historical 51 dimension/session differences in 17 parents, verify unavailable comparisons, add synthetic coverage, and repeat reconciliation.

**Approach:** Compare the read-only WD-195 snapshot with retained parent/child source records. Separate child membership and capture/import coverage from native cumulative-delta versus notification accounting. Correct only verified current defects, preserving raw records and unknown values.

**Scope:** Codex subagents; no provider calls, dependencies, schema changes, or changes to accounting solely to remove differences. Work in the current checkout on `feature/wd-223-codex-subagent-tokens`.

1. Reproduce all historical Codex subagent reconciliation rows. Verify child identities, source cursors, native receipts, and notification totals in ignored local artifacts.
2. Replay relevant retained records with the current parser/collector as needed to distinguish current defects from historical coverage. Quantify differences and limitations.
3. Add synthetic regression/characterization tests for verified causes. For a production fix, first demonstrate and commit failing tests, then implement the smallest correction and verify green.
4. Run focused tests, both full suites, applicable Ruff/ty/build checks, and independent review. Install only a verified production observation/control change under LIVE.md.
5. Record findings and checks in `docs/evidence/wd223.md`; move the ticket to DONE only when all requested deliverables are complete. Give deferred findings separate TODO IDs.
