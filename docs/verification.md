# Foundation verification

## WD-008 CLI and performance baseline

2026-09-06, Windows, CPython 3.12.13. New pytest cases first failed because the
project/doctor/sessions commands were absent. The full suite subsequently passed
148 tests, including the separate WD-025 counter fix. Ruff lint/format and ty
passed. Read commands use SQLite read-only snapshots without opening a Store or
running migrations; tests cover missing/corrupt/future/wrong-project databases,
pagination, session separation, and data preservation during registry changes.
Wheel and sdist built; a fresh offline wheel installation passed project add,
sessions list/show, doctor, and hook-to-daemon-to-SQLite capture/redaction checks.
The first smoke reached the SQLite file before table creation; adding a table
readiness check corrected the probe. Its daemon stopped and temporary state was
removed after both attempts.

The [synthetic baseline](evidence/wd008-windows-baseline.json) used two session
identities in one Git repository and a separate worktree: 40 sequential and 40
four-way concurrent calls plus one initial call. All 81 records survived daemon
restart, with zero recorded losses. Sequential p95 was 1089.5 ms; concurrent p95
was 1505.7 ms. This exceeds the 250 ms target. Idle CPU was 0.219 seconds over
20.609 seconds, with 34,414,592 bytes working set. Python/import profiling measured
roughly 80-87 ms for an empty interpreter and 478-542 ms including configuration
imports; config import cumulative time was about 349 ms in an import-time sample.
The user selected a small Rust adapter in WD-024 before WD-009, preserving the
Python core and inbox contract. These figures are a baseline, not target success.

The native CLI reported version 0.153.4; its interactive banner reported 0.153.3.
Two scratch hook files were prepared with the production two-second timeout.
The primary startup's native review trusted 11 definitions with user approval;
both CLI `/hooks` screens then showed 11 active handlers. The worktree did not
request a separate review; this does not establish that both files were loaded.
Two concurrent coding probes each ran one ten-second PowerShell command, with
intentional exit codes 0 and 1, and completed their requested reply.

Initial hook observations failed Git ownership validation: the scratch repositories
were created by the sandbox account and native hooks ran in a different account
context. Only the scratch roots/Git metadata ownership was corrected; no global
safe.directory exception was added. After that change, repeated native calls
reported `hook timed out after 2s`. No collected database was available at final
inspection, and adapter diagnostics recorded ten invalid observations. Those
counters do not enumerate every native timeout because a killed Python process
cannot reliably record its own failure. Concurrent native collection and desktop
event coverage therefore did not pass.

The user explicitly moved the remaining concurrent CLI/desktop and desktop event
matrix, plus target latency acceptance, to WD-024. WD-008 is closed for its CLI,
baseline, and documented failed live attempt; the failed acceptance is not erased.
Both probe TUIs exited, their tasks were archived, both hook files were uninstalled,
and the isolated daemon state was paused with `alive=false`. Native trust records
were not fabricated or copied. Other-OS hosts remain WD-019; remote CI was not run.

## WD-007 storage policy

2026-09-06 (local date), Windows, CPython 3.12.13. TDD first exposed missing
redaction, capture controls, retention/pin APIs, and quota admission. A later
concurrent rejection test exposed lost counter updates under lock contention;
bounded acquisition retry fixed it. Credential regressions cover quoted JSON,
Basic authentication, AWS secret assignments, and truncated private-key blocks.

Final offline verification: 140 pytest tests passed; Ruff lint/format and ty passed;
wheel and sdist built. Tests include 30/180-day expiry, pins, replay fingerprints
after content deletion, parallel inbox quota/counters, WAL and auxiliary-file
accounting, physical SQLite reclamation, stale temporary/orphan cleanup, v1
migration, and simulated ENOSPC publication with persistent loss recording.
Existing migration/commit/ack subprocess-crash checks also passed. No real disk
was filled, and no filesystem power-loss durability is claimed.

A fresh isolated environment installed the wheel offline. Its hook admitted a
synthetic Stop event, started a detached daemon, and stored assistant text while
removing a synthetic password. The probe used a path with spaces and closed its
SQLite reader; the owned daemon stopped successfully and temporary state was
removed. No provider hooks were installed and no LLM calls were made. Native
provider latency/remaining desktop evidence stays WD-008; macOS/Linux host checks
stay WD-019. Remote CI was not run.

## WD-006 Codex hooks

2026-09-06 (local date), Windows, CPython 3.12.13. TDD began with missing hook/installer modules. Offline verification: 123 pytest tests passed; Ruff lint/format and ty passed. Tests cover synthetic schemas reflecting WD-002 observations, bounded input, fail-open no-op output, pause/allowlist behavior, PowerShell metacharacters, exact original-file restoration, unrelated user edits, edited owned groups, missing ownership records, and interrupted-install recovery. Tests edit only inert temporary configuration files and do not invoke providers.

Wheel/sdist builds and an isolated installed-wheel hook-to-daemon-to-SQLite smoke passed, including a spaced path, content omission, and daemon stop. The first smoke reached those assertions but failed temporary-directory cleanup because its read-only SQLite connection remained open; the probe was corrected to close that connection and the complete run passed. This did not require a product change.

A later full run exposed a Windows empty-lock-file initialization race in the existing storage lock. Initial byte writes now participate in contention handling and use an unbuffered handle, so a denied initialization write is reported as `WriterBusy` without being retried implicitly on close. A Windows-specific regression test and the concurrent registry test cover this fix. A separate installer regression protects invalid ownership records such as JSON `null`.

Live Codex CLI version command reported 0.153.4; the interactive banner reported 0.153.3. A scratch Git project with spaces in its path was explicitly registered. All 11 generated two-second hooks were reviewed through native UI with user authorization. The CLI run recorded session start, prompt submission, two pre/post tool pairs for `exit 0` and `exit 1`, Stop, and SessionEnd after `/quit`. Both tool results remained opaque strings with unknown outcome; no exit-code parser or success claim was added.

The first desktop resume completed but produced no events: native trust had been saved under `--profile lean`, outside the base configuration used by desktop. A second native review of the same definitions without the profile resolved this. Reloading the archived task through desktop then recorded SessionStart, a pre/post tool pair, and Stop. The adapter correctly retained unknown surface/version instead of inferring them from the test controller. Remaining desktop event coverage and measured hook latency remain WD-008.

Before uninstall, aggregate event counts were: session.start 2, session.end 3, turn.start 1, turn.end 2, tool.start 3, tool.finish 3. Counts include the base-profile trust-only session and are not unique task counts. Uninstall removed the created hook file; after a fresh desktop load and no-tool turn, the count remained 14. All persisted envelopes were checked to contain the metadata-only policy marker. The owned daemon was stopped, both probe tasks archived, and both TUI processes exited. Native trust records were not fabricated, copied, or cleared. No global hooks were installed, no real content was retained, and no other-OS or remote CI result is claimed.

## WD-005 daemon

2026-09-05, Windows, CPython 3.12.13: initial new tests failed because the daemon module was absent. Focused tests exposed stale status during restart and a Windows venv-launcher limitation in external crash simulation; status is invalidated before detached launch, and the crash probe terminates the actual interpreter with `os._exit`. Final verification: 81 pytest tests passed without skips, Ruff lint/format and ty passed, wheel/sdist built, and the installed wheel started and stopped its detached daemon from a path containing spaces. Tests cover independent startup contenders, persistent pause, serialized registry updates, allowlist admission, stale PID/heartbeat, immediate stop/start, project failure isolation, and crash recovery. Live CLI exit and desktop session unload evidence and explicit limits are recorded in [daemon.md](daemon.md). The user accepted desktop session unload for WD-005; no full desktop shutdown, other-OS host, or remote CI result is claimed. Test-owned daemons were stopped through their isolated control files.

2026-09-05, Windows, isolated CPython 3.12.13 via uv 0.11.7. For sandbox runs, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` point to the ignored project `.cache` directory; this is an environment workaround, not a product requirement.

| Check | Result |
|---|---|
| TDD red: pytest before package creation | Both tests failed because agent_watchdog was missing |
| `uv run pytest -q` | 2 passed; cache warning due to sandbox permissions |
| `uv run pytest -q -o cache_dir=.cache/pytest` | 2 passed, no warnings |
| `uv sync --locked` | PASS |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS |
| `uv build` | Wheel and sdist built |
| `uv run agent-watchdog --help` | PASS; explicitly states that collection is not implemented |
| `uv run --isolated --no-project --python 3.12 --managed-python --with ./dist/agent_watchdog-0.1.0-py3-none-any.whl agent-watchdog --help` | PASS; built wheel installed in an isolated environment and its console entry point executed |
| `git diff --check` | PASS for the tracked diff |

The new text files were also checked for local Markdown links, trailing whitespace, and UTF-8/LF: 17 files, PASS. This was a documentation check, not a product test suite.

At M0 acceptance, the CI matrix was configured but remote CI, macOS/Linux, and live vendor hooks had not been tested. Fixtures or documentation alone do not establish runtime support. Subsequent live results are in [the WD-002 report](provider-compatibility.md). Completed task records live in [DONE.md](../DONE.md).

## M0 acceptance audit

The local checks above were rerun after the English documentation update: 2 tests passed without warnings, locked dependency synchronization and Ruff passed, and wheel/sdist builds succeeded. The isolated wheel check verifies the distribution rather than relying solely on the editable development installation.

Reviewed the CI matrix and read-only permissions, source links and explicit integration limitations, task dependencies and acceptance criteria, and the separation of open and completed work. All M0 deliverables are present. Windows live provider evidence remains M1 work; macOS/Linux host access and compatibility validation are deferred to M5 / WD-019; no remote CI run or runtime collection is claimed.

## WD-002 capture probe

2026-09-05: the five new capture test cases first failed because the script did not exist, then passed after implementation. Full suite: 8 passed; Ruff lint and formatting passed. Tests cover malformed input, structural capture without content, identity correlation, and fail-open storage errors with a Codex no-op response. Live hooks are opt-in and never run in pytest. See [provider compatibility](provider-compatibility.md) for actual surface coverage, failed diagnostic attempts, and limitations.

## WD-003 contracts

Windows, CPython 3.12.13: initial new tests failed at collection because the config/registry modules were absent. Final verification: 42 tests passed without skips, Ruff lint/format and ty passed, `uv sync --locked` passed, wheel/sdist built, and the contracts imported from an isolated wheel installation. Tests exercised real temporary Git repositories, a linked worktree and clone, symlink/case aliases, explicit relocation, project overrides, protected invalid configs, atomic-save failure, and nullable event identifiers. The existing detach probe exposed a Windows working-directory cleanup race during the full run; WD-023 fixed it and the full suite passed afterward. No live provider probes or remote CI runs were performed. Concurrent configuration writers are not implemented; callers must serialize mutations until the daemon/CLI workflow provides coordination.

## WD-004 storage

Windows, CPython 3.12.13: initial storage tests failed because the module was absent. Focused red tests also exposed ingestion without an open writer and missing persisted replay identity; both cases are now rejected. Final checks: 68 tests passed without skips, Ruff lint/format and ty passed, wheel/sdist built, and an isolated installed-wheel inbox-to-SQLite smoke passed. Tests used real SQLite transactions and test-owned process termination before event COMMIT, after COMMIT/before acknowledgement, and before initial migration COMMIT. They also verified single-writer exclusion, released locks after crashes, read-only access, protected newer/unrelated schemas, project/UUID conflicts, bounded quarantine, symlink input handling, and artifact failure/integrity. Power-loss durability and other-OS host behavior were not tested. No remote CI or live provider run was performed.
