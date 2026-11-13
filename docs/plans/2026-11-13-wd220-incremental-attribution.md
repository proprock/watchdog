# WD-220 Incremental Attribution Refresh Implementation Plan

**Goal:** Refresh late model and effort attribution in imported hook facts without requiring a full import.

**Architecture:** Keep the append-only event cursor and add a bounded cyclic refresh of previously imported facts. Store the refresh cursor and a fixed cycle high-water mark in the existing import_state table under a separate machine/project key. Update only the four attribution columns, atomically with both cursors. Freeze each cycle's boundary so continuous appends cannot prevent revisiting old facts.

**Tech Stack:** Python, existing Watchdog readapi, SQLite, pytest.

## Acceptance and scope

- Offline late-context regression through the real Store proves incremental import repairs attribution while retaining raw events and token totals.
- Each import refreshes at most 500 historical facts; persisted progress resumes across calls and cycles even while events grow.
- Failures roll back attribution and cursor changes; full import resets refresh progress.
- No schema changes, source-writer changes, provider probes, or token reconciliation changes.

## Decision

A source revision journal would offer immediate updates but requires writer/schema changes and installed-store conversion. A full rescan on every import has unbounded cost. Use the bounded sweep through the current read contract instead; eventual visibility takes one finite cycle (at most ceil(facts / 500) + 1 imports for a fixed boundary).

## Steps

1. Add regressions in `insights/tests/test_hook_import.py`: real late context, bounded cycling with concurrent growth, refresh rollback, full reset. Run focused tests and confirm behavioral failures. Commit failing tests alone with `TDD: red`.
2. Implement the bounded refresh in `insights/src/wdi/ingest/hook_facts.py`, preserving the new-event count and atomic per-store import. Run the focused module and commit the solution with `TDD: green`.
3. Document refresh semantics in `docs/read-contract.md` and `insights/README.md`; record checks in `docs/evidence/wd220.md`; move WD-220 from TODO to DONE only after acceptance.
4. Run both pytest suites, applicable Ruff/format/type/build checks and `git diff --check`. Review the final diff and audit the red/green pair. No push requested.
