# WD-231 Daemon Read Races Implementation Plan

**Goal:** Preserve capture readiness checks and policy discovery through concurrent publication on Windows.

**Architecture:** Wait for an actually committed capture record in the test without treating a permanently invalid store as ready. Publish discovery with the existing shared-reader replacement primitive and use cooperative shared reads in Python; retain the native adapter's fail-open contract and subscription assertions.

**Tech Stack:** Python, SQLite, Windows file sharing, pytest, Rust adapter.

1. Characterize capture initialization and Windows discovery replacement with deterministic tests; verify the relevant pre-fix failures and commit the red tests.
2. Apply the smallest readiness and publication/read changes. Preserve bounded failure and old-handle snapshots; do not suppress permanent errors.
3. Run focused tests, the full Watchdog suite, applicable static and native checks, and inspect the final diff. Record observations and move WD-231 only when complete.
