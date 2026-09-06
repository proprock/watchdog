# Foundation verification

## WD-022a Claude observation (gate not met)

2026-09-06, Windows, CPython 3.12.13, Claude Code 2.1.259. Branch
`feature/wd-022a-claude-observation`. WD-022a implements Claude observation only:
a provider-parameterized Python and Rust adapter, a `settings.json` /
`settings.local.json` installer that emits exec-form entries with `timeout: 2`,
and offline tests. Transcript enrichment, usage reconciliation, and
guidance/control delivery are deferred to WD-022b.

Offline: the full pytest suite, Ruff lint/format, ty, `uv build`, and
`cargo fmt`/`clippy`/`build --release` pass. New coverage: the 12-event Claude
map for both adapters, empty stdout for Claude on every path (success,
unregistered project, malformed JSON, oversized payload, pause, storage
failure), redaction of `prompt` and `tool_response`, `capture_content = false`,
Rust/Python envelope agreement for all 12 Claude events, unknown-provider
refusal, exec-form installer round trips, realistic multi-key `settings.json`
preservation, a legacy Codex manifest without `provider`, and cross-provider
file/path mismatches.

### Live Claude CLI pass

Isolated Watchdog `--home`, a scratch Git repo with a space in its path plus a
linked worktree (one project, two checkouts), the release binary copied to a
stable path, and Claude hooks installed with `--adapter-executable` and
`timeout: 2` into an isolated `settings.json`. The user's real
`~/.claude/settings.json` SHA-256 was recorded before the run and is unchanged
after it (`b84accab…9bc`). No native trust record was edited. All state is under
the ignored `.cache/wd022a-live/`. [Sanitized aggregate
evidence](evidence/wd022a-windows-claude.json) carries counts, kinds, durations,
and outcome tallies only.

- **Adapter behaviour is correct under real Claude CLI.** Four concurrent
  `claude -p` sessions (`--settings <iso> --setting-sources ""`,
  `--allowedTools Bash --permission-prompts none`), each running two Bash tool
  calls including one non-zero exit, drove all relevant hooks. Every hook
  returned `outcome: "success"` with empty stdout (28/28), `PostToolUseFailure`
  mapped to a stored `tool.finish`, and every one of the 8 `tool_use_id` values
  in the streams has a matching stored `tool.finish` envelope. The stored set of
  31 event ids is byte-identical before and after an isolated daemon restart.
- **One reliability gap.** One `SessionStart` was dropped under four-way
  concurrent session start: the adapter's `invalid` loss counter incremented
  (fail-open and counted), and one session stored 7 events instead of 8. A
  single sequential probe run produced further `invalid` losses. This is a real
  concurrent-delivery defect, not a measurement artefact.
- **Harness-side hook duration could not be measured on this build.** Claude
  Code 2.1.259 does **not** write `hookInfos` / `durationMs` into `claude -p`
  JSON transcripts, so `scripts/hook_timing.py` finds no durations there (it
  still extracts `tool_use_id`s correctly). The `--include-hook-events` stream
  emits paired `hook_started` / `hook_response` records with no timestamp or
  duration field. Timestamping those records on read
  (`scripts/hook_stream_timing.py`) gives a four-concurrent p95 of 515 ms but a
  **sequential** single-session p95 of 1906 ms — an inversion that shows the
  stream delta is dominated by Claude's stdout-flush and event-loop cadence, not
  by hook wall time. This method is not a trustworthy substitute for the
  transcript number the gate is written against.
- **Adapter launch cost, measured synthetically.** `scripts/benchmark_hooks.py`
  against the same binary, isolated, 20 samples
  ([evidence](evidence/wd022a-claude-launch.json)): direct exec form
  four-caller p95 **153 ms** (max 199 ms); `bash -c` four-caller p95 **240 ms**.
  PowerShell 7 (`pwsh.exe`) is not installed on this host; WD-024 retains the
  Windows PowerShell 5.1 and PowerShell 7 shell numbers for the same adapter.
  The adapter's own exec-form launch is within the 250 ms budget; the shell
  wrappers are at or above it, which is why the installer emits exec form.

### Gate status (strict, per the WD-022a plan)

| # | Criterion | Result |
|---|---|---|
| 1 | Harness-reported `durationMs` p95 ≤ 250 ms in passes A and B | **Not demonstrated.** The build does not expose the number; the stream-delta proxy is unreliable. Synthetic direct-exec launch p95 is 153 ms. |
| 2 | Every `tool_use_id` has a stored `tool.finish` | Pass for the concurrent CLI pass (8/8). Desktop not run. |
| 3 | Real desktop composer submission produces a stored `turn.start` | **Not run.** The supervised desktop pass is pending. |
| 4 | Stored event id set survives a daemon restart, losses zero | Restart preserved (31 ids). **Losses not zero:** one `invalid` adapter loss in the concurrent pass. |
| 5 | Nothing satisfied by a raised timeout, async backgrounding, or a relaxed deadline | Pass. `timeout: 2`, exec form, fully synchronous throughout. |

**WD-022a does not close.** Criterion 1 depends on a harness measurement this
Claude build does not produce, and criterion 4 has a real concurrent-delivery
loss. Per the plan, the threshold is not renegotiated and async delivery is not
adopted to get past it. The disposition — pursue a reliable timing source and
fix the concurrent `invalid` loss, or redefine the gate for what 2.1.259
exposes — is the user's decision, made after reading this section and the
[per-provider blocker table](../tmp-WD-024.md). The offline implementation and
the isolated-daemon behaviour stand; Pass B (co-resident user hooks), Pass C
(shell attribution against a live Claude session), and the supervised desktop
pass were not run.

## WD-024 Rust adapter (acceptance remains open)

2026-09-06, Windows, CPython 3.12.13, Rust 1.98.1. The interrupted implementation
was recovered on `feature/wd-024-rust-adapter`. Release build and all 172 offline
pytest tests passed, including Rust/Python envelope, privacy, pause, allowlist,
quota, compatible lock, concurrent publication, copied-binary/core startup, and
worktree contracts. Cargo fmt/clippy, Ruff lint/format, ty, and wheel/sdist build
passed. The native binary remains separate from the portable Python wheel.
No remote CI or other-OS runtime result is claimed.

The installed Rust binary's [original baseline](evidence/wd024-windows-baseline.json)
is retained. A [direct recheck](evidence/wd024-windows-direct-recheck.json) confirms
the adapter target. Shell measurements use the same installed binary and generated
Windows command, with `-NoLogo -NoProfile -NonInteractive -Command`:

| Launch | First call (ms) | Sequential p95 (ms) | Four callers p95 (ms) |
|---|---:|---:|---:|
| Direct Rust recheck | 100.3 | 110.7 | 225.7 |
| [Windows PowerShell](evidence/wd024-windows-powershell.json) | 324.3 | 338.8 | 833.0 |
| [PowerShell 7](evidence/wd024-windows-pwsh.json) | 2147.1 | 722.9 | 1375.2 |

Each successful run admitted 81 synthetic events across two sessions/checkouts,
preserved them across core restart, and recorded zero losses. These diagnostic
subprocesses have a **15-second** timeout, not the installed hook's two seconds.
The first PowerShell 7 call exceeded that native budget; repeated short shell
diagnostics also measured first calls of 2125 ms before interruption and 2101 ms
after recovery. This identifies shell startup as a material cost and a plausible
timeout source; it does not attribute every missing native event to that cause.
Shell choice here is a benchmark option, not a Codex configuration override.
The 250 ms end-to-end target is not demonstrated through either measured shell.

One direct benchmark attempt failed with `KeyError: 'pid'`: detached startup can
invalidate the status snapshot before a live PID is published. The benchmark now
waits for a healthy live PID before timing, reading idle metrics, and inspecting
restart results. Two behavioral regressions first failed, then passed, including
bounded rejection of a stale PID. The corrected installed-binary run passed.
The first full pytest run had a sandbox cache-directory warning; the final run
used the writable `.cache/pytest` and passed without warnings.

### Native observations and remaining gates

[Sanitized aggregate evidence](evidence/wd024-windows-native.json) excludes content,
native identities, and the synthetic shell-timing session. The original CLI and
desktop observations remain in the aggregate; the recovery run has separate
counts. The adapter does not infer the surface from a session's cwd. Attribution
below comes from the probe controller, not from envelope metadata.

- CLI: the npm-installed executable reports 0.153.3 in the TUI. A stock `exec
  resume` probe captured session/prompt start, a pre/post pair, Stop, and SessionEnd.
  The separate desktop `codex` command reported 0.153.4 but could not find the
  saved CLI rollout, so that failed resume was not treated as a provider test.
- Concurrent CLI/desktop: both 30-second tools and their requested replies
  completed. Both checkouts recorded tool start and turn end, but neither recorded
  the corresponding PostToolUse in this run. The adapter's project counters were
  zero; this does not establish lossless native delivery. The earlier run explicitly
  reported two-second native hook timeouts. The recovery run did not reproduce an
  explicit timeout message, and the missing callbacks remain unexplained.
- Desktop: the original run captured session start, an `exit 1` tool completion,
  subagent start/stop, and Stop. Recovery again captured an `exit 1` completion.
  Task archival/unload captured SessionEnd. Follow-ups sent through the desktop
  task API did not produce UserPromptSubmit; actual composer submission remains
  unverified. Desktop interrupt/compaction controls were unavailable through the
  current tools, so CLI observations do not establish their desktop coverage.
- CLI lifecycle: Escape during a 45-second tool produced Interrupt. `/compact`
  completed and produced PreCompact/PostCompact. `/quit` produced SessionEnd.
  `/hooks` showed all 11 handlers active. No hook timeout or trust setting was
  increased, copied, or bypassed.
- Restart: the exact set of stored live event IDs survived an explicit stop/start
  of the isolated daemon. Raw local observations remain under ignored test state.

Cleanup completed: both scratch hook installations were removed using their
ownership records, the probe tasks were archived, the owned TUI exited, and the
isolated daemon reported `paused` with `alive=false`. Native trust records and
the local diagnostic data were retained; no global hooks were installed.

WD-024 remains open for reliable native concurrent delivery, actual desktop prompt
submission and remaining desktop lifecycle triggers, and end-to-end latency.
The direct Rust target passing is not sufficient to close these gates. Next
diagnostics need native runner start/exit/timeout evidence for each missing callback;
the current counters cannot distinguish a callback that never ran from a process
killed before publication. Any change to the native deadline or launch contract
must be evaluated separately from the adapter benchmark.

The WD-022a live Claude CLI pass exercised the **same binary** on a harness that
reports its own hook lifecycle. It confirms exec-form launch under four-way
concurrency at p95 153 ms synthetically, reproduces a concurrent-delivery
`invalid` loss (one dropped `SessionStart`), and finds that Claude Code 2.1.259
does not expose a per-hook `durationMs` for `claude -p`. See
[WD-022a Claude observation](#wd-022a-claude-observation-gate-not-met) and the
[per-provider blocker table](../tmp-WD-024.md); what the Claude evidence does and
does not prove about Codex is stated there.

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
