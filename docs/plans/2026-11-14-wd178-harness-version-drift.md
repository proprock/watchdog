# Harness Version Drift Implementation Plan

**Goal:** Warn when a Codex hook's installed control profile differs from the backend version observed in that session's captured metadata.

**Architecture:** Preserve the adapter's profile as trusted delivery metadata. Compare it asynchronously with a bounded read of the first captured record for the same source and session. Materialize a finding with event evidence and expose diagnostic warnings; retain unknown when evidence is missing or unreadable. Capture progress must schedule a refresh even without another hook event.

**Tech Stack:** Python 3.14, SQLite, Rust, pytest.

## Acceptance and verification

- Exercise profile propagation with telemetry both enabled and disabled.
- Exercise matching, differing, absent, malformed and unreadable metadata, including sealed and unsealed captured records and bounded decompression.
- Verify asynchronous capture refresh, finding evidence, status and doctor warnings, and unchanged control and kill switches with offline tests.
- Run the focused modules, full root suite, applicable static and native checks, and record results in the evidence file. Update the local instance only after verification, as required by TENETS.md.

## Sequence

1. Add failing tests, then preserve the actual adapter profile through spool ingestion.
2. Add failing tests, then implement the bounded source-specific metadata comparison with explicit unknown results.
3. Add failing tests, then connect the comparison to findings, schedule refresh after capture and expose status/doctor notices.
4. Update the operator documentation, run checks, inspect the diff and record evidence.

No provider calls, new control actions, provider-file reads, or changes to kill-switch behavior are authorized by this task.
