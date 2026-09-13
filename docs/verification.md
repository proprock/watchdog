# Foundation verification

## WD-012 calibration (in progress)

2026-09-07: the first manual calibration pass reviewed 20 completed root Codex
coding sessions from 2026-08-24 through 2026-09-07. The auditable, content-free
record is [wd012-calibration.json](evidence/wd012-calibration.json): it retains
only source session IDs, start times, repository names, manual task/outcome
labels, and local source hashes where the source was not locked. Raw rollout
JSONL, prompts, assistant output, absolute paths, and provider credentials are
not copied into this repository.

The convenience sample is deliberately not representative: it contains 11
`ventFather`, 6 `watchdog`, and 3 `agent-session-insights` sessions. Manual
outcomes were 16 `success` and 4 `partial`; its typical work was feature delivery
(11), hardware validation (4), debugging/investigation (3), and research/design
(2). This establishes a review protocol and a task/outcome baseline, not a
claim that a completed turn or tool success proves task success.

The local Watchdog store had no registered project or captured sessions at
review time. Therefore its shadow findings, false-positive count, precision, and
per-hook overhead are **not evaluable** for this sample; they are recorded as
`null`, never as zero. Earlier synthetic adapter benchmarks remain separate
evidence and do not measure these real sessions. WD-012 remains open until a
registered Watchdog capture provides 20--50 sessions with report findings and
timing data. The next pass must label outcomes through the existing CLI, retain
each finding's `true_positive`/`false_positive`/`uncertain` review, report the
denominator for precision, and preserve the same no-automatic-harness-change
boundary. That verdict vocabulary supersedes the `correct`/`false_positive`/
`unknown` wording used above for the first pass; `uncertain` here is the
correctness of the finding and is unrelated to a finding's `attribution` field.

The tooling for that pass is `scripts/calibrate.py` with storage schema v7. It
freezes a reproducible cohort (`sample`), reviews it against the collected store
(`annotate`), and emits `wd-012.calibration.v2` evidence plus a markdown report
(`report`). Sampling records its seed, strata, and skip counts so the dataset
definition is auditable; annotations are written through the existing control
inbox, never by the reviewing process itself.

## WD-012 annotation tooling (2026-09-09, Windows)

Storage schema v7 adds the manual annotation fields: `session_labels`
`progress_state` and `reviewer_note`, plus `finding_verdicts` and
`checkout_finding_verdicts`. Findings now carry a SHA-256 `fingerprint` over
their sorted evidence identifiers, so a verdict survives recomputation and stops
applying when the evidence set changes; `REPORT_SCHEMA_VERSION` is 2. The CLI
gains `label --progress/--note` and a `verdict` command, both routed through the
existing control inbox; the CLI still opens no write connection.
`scripts/calibrate.py` provides `sample`, `annotate`, and `report`.

Offline verification: 407 pytest tests passed in three bounded groups (82
storage/analysis, 74 daemon/CLI/query, 251 remaining), covering the v6-to-v7
upgrade, label-field preservation, verdict validation and purge, the two new
control actions and their rejection path, fingerprint stability, seeded sample
reproducibility, CSV/JSON agreement, reviewer navigation without writes, and the
report math. Ruff lint and format, ty, `uv build`, `cargo fmt --check`,
`cargo clippy --locked -- -D warnings`, the locked release build, and
`git diff --check` passed. No provider was invoked.

One test initially reached the real control path, and `daemon.request_control`
starts a daemon, so each run left a detached daemon in its temporary home. The
leaked processes were identified by their `C:	mp` config path, stopped, and the
test now stubs the control call; the live instance was never a target. A separate
run of the daemon/CLI group showed one timeout in the pre-existing
`label`/`pin`/`export`/`purge` test when the host was loaded: its control
acknowledgement has a ten-second budget. It passed alone and on a repeat of the
group; it is recorded here as an observed flake under load, not a silent pass.

Live capture, same day: the owned daemon was stopped, `events.sqlite3` was copied
to a backup outside the checkout, the adapter binary and wheel were replaced, and
the daemon was restarted. The store upgraded from `user_version` 6 to 7 with both
verdict tables present and 7639 events preserved. Daemon status remains
`degraded` for the pre-existing transcript-enrichment failures
(`transcript_unreadable`, `session_meta_mismatch`, `rollout_line_invalid`) with
all loss counters at zero; that state predates this change. A `verdict` request
for a nonexistent session was acknowledged as `Unknown session in the selected
provider`, which shows the action reaches the storage layer without writing a row
into the dataset.

`sample` froze a 24-session cohort in [wd012-sample.json](evidence/wd012-sample.json)
from 48 considered sessions (20 below the 20-event floor, 4 not settled): 4 with
a session-scoped finding and 20 drawn under seed 12 from the 20 with none. The 8
`diff_oscillation` findings are listed once at checkout scope instead of the 384
per-session instances the rule produces across this single-checkout capture.

Measured on that live data, per-hook cost is p50 5.3 ms and p95 15.0 ms in the
hook, and p50 842.4 ms / p95 2084.3 ms end to end, over 3033 of 7648 events;
transcript-sourced events carry no delivery trace, so the remaining 4615 are not
measured rather than fast.

**No manual review has been performed yet.** `repeated_tool_outcome` is observed
8 times and `diff_oscillation` 8 times, both unreviewed; `identical_error` and
`repeated_test_failure` have zero observations in this window. Every precision is
`null` with its reason distinguished ("no observation" from "observed but not
reviewed"), never zero and never 100%. [wd012-calibration.json](evidence/wd012-calibration.json)
still holds the first pass and is not replaced until the review produces real
counts. WD-012 stays open.

## WD-012 review card (2026-09-13, Windows)

The `annotate` card was the bottleneck for the manual pass, not the storage. It
showed only the first/last prompt, a truncated last message, and up to three
repeated tool inputs, so the causal chain between them was invisible; `d` printed
the whole report JSON including hundreds of event UUIDs; a finding offered its
evidence as identifiers; and nothing on screen said whether a missing message
meant silence or a missing capture.

The card now reads as work. `build_timeline` collapses each tool start and finish
into one row with the command, outcome, exit code, duration, and the text the
tool returned, and `l` widens that tail (`l 50`, `l all`). Token-counter `usage`
events are excluded. Where a provider reports no exit code the rules classify the
result as unknown, so the row prints what came back instead of a guessed outcome;
a present but empty output field reads as `no output`. A `content` line counts prompts, replies,
and tool results as stored, empty, or not stored, so an empty answer stays
distinct from an absent capture. A finding prints its evidence as those timeline
rows. `d` prints the live re-analysis in readable form and says it is live; `dj`
keeps the raw JSON. `x` offers to open the vendor transcript with the system
viewer, but only an existing `.jsonl`/`.json`/`.log`/`.md`/`.txt` path, because
the path itself comes from an untrusted trace. The review resumes at the first
unlabelled session.

The card no longer repeats a miscounted failure. `summarize` counted a tool
failure whenever the string `PostToolUseFailure` appeared anywhere in the stored
payload, so a session whose own pytest output names that hook was recorded as
having failed: the first card of [wd012-sample.json](evidence/wd012-sample.json)
claims 4 failed tool calls while none of its 53 stored results is a failure. The
count now follows the provider's own signal, and the card shows the observed
result mix and flags a frozen count that disagrees with it. The frozen cohort is
not regenerated, so its `tool_failures` column keeps the old value; the review
reads the card, and the calibration report never consumed that column.

Recording an answer no longer discards the previous one. Each keystroke sent
only the field it set, and the store replaces `task_outcome` and `task_type` with
whatever the request carries, so pressing `o` after `t` cleared the task type.
The review now resends the whole label with one field changed; a scripted
`t`/`o`/`p` sequence covers it.

The card also states the frozen/live boundary it previously left implicit: the
listed findings are the sample's, verdicts attach to their fingerprints, and a
line reports whether a live re-analysis would now add or drop any. The reviewer
reported nine checkout-scoped findings from a live report against the eight
frozen in [wd012-sample.json](evidence/wd012-sample.json); that count is not
measured here, and it is exactly the divergence the indicator now names.

Offline verification: the full suite passed, 439 tests in 101 s with a base-temp
directory outside the checkout, after the locked release build of the native
adapter. Ruff lint, Ruff format, and ty passed. New behavioural tests cover the
collapsed tool row, an unfinished call, the provider-error fallback when a result
was not stored, the stored/empty/not-stored counts, the frozen-versus-live drift
line, evidence rendered as rows rather than identifiers, a live detail view that
prints no event identifier, resuming at the first unlabelled session, and the
refusal to launch a non-transcript path named by a trace. No provider was
invoked, no hook was installed, and no manual review was performed; WD-012 stays
open.

## WD-011 labels, pins, and manual export

2026-09-07, Windows. WD-011 adds storage schema v5 with a provider-scoped
session outcome/type label. `label`, `pin`/`--unpin`, and `purge` send bounded
request files to the core and wait for durable acknowledgements, preserving the
single-writer contract. The offline `export` command selects explicit sessions
and writes JSONL, Markdown summary, versioned manifest, and a manual analysis
prompt; its manifest preserves labels, counts, gaps, redaction limits, and a
recommended content review before external sharing. Purge is scoped
to Watchdog-owned data for one provider/session and never reads or modifies a
vendor transcript or a project file.

Offline verification passed: 278 pytest tests in four bounded groups (56
analysis/provider contracts, 79 daemon/hook contracts, 104 remaining Python
storage/inspection tests, and 39 Rust-adapter tests), plus 41 focused
CLI/storage/export tests. Ruff lint/format, ty, `uv build`, locked Rust release
build, `cargo fmt --check`, locked Clippy with `-D warnings`, and `git diff
--check` passed. No live provider probe was run.

## WD-010 shadow findings and reports

2026-09-07, Windows. WD-010 adds a read-only `report` surface over stored
observations. The deterministic `wd-010.v1` rules retain only evidence IDs and
fingerprints in their findings: three matching tool outcomes, structured errors,
or comparable pytest/JUnit failing sets; an A-to-B-to-A Git diff signal is always
marked uncertain attribution. Report output keeps lifecycle, usage, and content
coverage explicit; neither zero exit, waiting, nor Stop is a task verdict.

Offline verification passed: 276 pytest tests in three bounded batches (60 core,
149 contracts/hooks/transcripts/retention, 67 Rust adapter/tooling), plus three
focused post-change analysis tests; Ruff lint/format, ty, `uv build`, locked Rust
release build, `cargo fmt --check`, locked Clippy with `-D warnings`, and `git diff
--check`. No live provider probe was run.

## WD-009 Codex transcript enrichment

2026-09-07, Windows, CPython 3.12.13. Branch
`feature/wd-009-transcript-enrichment`. WD-009 implements asynchronous Codex
only enrichment: a daemon reads exact, hook-provided transcript paths through
the version-gated `codex-rollout-v1` reader. The hook never opens a transcript.
The reader validates `session_meta` and the expected session before accepting
`token_usage_record` data, persists no transcript content, and reports
unsupported or malformed sources as durable `observation.gap` records.

Schema v3 persists reader identity, offset, partial UTF-8 tail, canonical
`thread_token_usage` counters, and last reported failure. Coverage uses
synthetic JSONL only and includes normal enrichment/replay, field availability,
hook/session reconciliation, partial tails, malformed and unsupported inputs,
truncation/replacement, counter resets, duplicate records, capture disabled,
unreadable sources without blocking normal admission, v1/v2 migration,
retention, and read-only inspection.

Offline verification passed: 271 pytest tests in five bounded batches (the
execution host returns terminal control at roughly 30 seconds per command),
Ruff lint and format check, ty, `uv build`, `cargo fmt --check`, locked Clippy
with `-D warnings`, locked release build, and `git diff --check`. No live
provider probe was run.

## WD-022a Claude observation (gate met)

2026-09-06, Windows, CPython 3.12.13, Claude Code 2.1.259 then 2.1.263 (auto
update mid-testing). Branch `feature/wd-022a-claude-observation`. WD-022a
implements Claude observation only: a provider-parameterized Python and Rust
adapter, a `settings.json` / `settings.local.json` installer that emits exec-form
entries with `timeout: 2`, and offline tests. Transcript enrichment, usage
reconciliation, and guidance/control delivery are deferred to WD-022b.

Offline: the full pytest suite, Ruff lint/format, ty, `uv build`, and
`cargo fmt`/`clippy`/`build --release` pass. New coverage: the 12-event Claude
map for both adapters, empty stdout for Claude on every path (success,
unregistered project, malformed JSON, oversized payload, pause, storage
failure), redaction of `prompt` and `tool_response`, `capture_content = false`,
Rust/Python envelope agreement for all 12 Claude events, unknown-provider
refusal, exec-form installer round trips, realistic multi-key `settings.json`
preservation, a legacy Codex manifest without `provider`, cross-provider
file/path mismatches, filesystem checkout resolution proven against live
`git rev-parse` across six layouts, and attributable adapter-fault evidence.

### Part 2 fix — git subprocess off the hot path, attributable faults

The first live CLI pass dropped one `SessionStart` under four-way concurrent
session start (a counted `invalid` adapter loss), and Claude Code 2.1.259 does
not write `hookInfos` / `durationMs` into `claude -p` transcripts. Two changes
followed.

- **Checkout resolution from filesystem reads.** `git_checkout` (Rust,
  `native/src/main.rs`) and `discover` (Python, `src/agent_watchdog/registry.py`)
  resolve the toplevel and git-common-dir by reading `.git`, a worktree `.git`
  file's `gitdir:` line, and a `commondir` file — producing the same values as
  `git rev-parse --path-format=absolute --show-toplevel --git-common-dir` across
  repo root, subdirectory, linked worktree, worktree subdirectory, non-Git
  directory, and a path with a space. `git` is spawned only for an unrecognized
  layout. This removes one process spawn per hook — the strongest root-cause
  candidate for the concurrent loss — and is proven against live `git` output in
  `tests/test_checkout_resolution.py` and cross-language in
  `tests/test_rust_adapter.py`.
- **Attributable fault evidence.** An `observe()` failure still increments the
  `invalid` counter and now also appends one content-free line
  (`<timestamp> <event> <reason-category>`) to a rotating, 32 KiB-capped
  `faults.log` under the data directory. Reason categories only (`git-timeout`,
  `config-race`, `io-error`, …); never prompt, path, command, or tool output.

Removing the spawn roughly halved the launch benchmark: direct exec form,
20 samples, four concurrent callers **153.2 → 96.4 ms p95** (max **199.4 →
104.6 ms**), sequential **82.4 → 36.4 ms p95**
([evidence](evidence/wd022a-claude-launch.json)).

### Revised criterion 1 — Watchdog's own blocking cost

Claude Code exposes `hookInfos.durationMs` only in **interactive** transcripts,
and only for the `Stop` hook (`subtype: stop_hook_summary`); `claude -p` writes
no such record. Criterion 1 is judged on the quantity Watchdog controls — its
own blocking cost — measured by the launch benchmark (1a), with the interactive
`Stop` `durationMs` as corroboration (1c).

**Stated limitation (recorded verbatim per the WD-022a plan):** Criterion 1 no
longer includes Claude's own hook-dispatch overhead. Two reasons it is safe:
that overhead is not exposed on this build, and it applies to any hook the user
installs — it is not Watchdog's cost and Watchdog cannot reduce it. 1a is a
synthetic launch-cost measurement, not an end-to-end harness measurement, and is
not presented as one.

### Live re-verification

Isolated Watchdog `--home`, a scratch Git repo with a space in its path plus a
linked worktree, the release binary at a stable path, hooks installed with
`--adapter-executable` and `timeout: 2`. All state under the ignored
`.cache/wd022a-live/`; no native trust record edited. The recorded
`~/.claude/settings.json` baseline moved from `b84accab…9bc` to `b03519ab…a2d3`:
Claude Code rewrites its own global settings file (default-key normalization) on
every launch. No Watchdog code writes under `~/.claude`, and the file's content
was user-reviewed as unchanged apart from that normalization; the SHA is no
longer treated as an isolation check. [Sanitized
aggregate](evidence/wd022a-windows-claude.json) carries counts, kinds,
durations, and outcome tallies only.

- **Stage 1 — interactive `durationMs` probe.** One interactive session, isolated
  settings. `hookInfos.durationMs` present for `Stop` only: 51 / 60 / 71 ms
  (p95 71). Watchdog store: 18 events, zero losses, no `faults.log`.
  `scripts/hook_timing.py` keeps its purpose (interactive transcripts, Stop
  hooks); the untrusted read-time stream delta was removed from
  `scripts/hook_stream_timing.py` (schema 2).
- **Stage 3 — Pass A re-run (`--model claude-haiku-4-5`).** Four concurrent
  `claude -p` across the repo and its worktree, `--setting-sources ""`, two Bash
  calls each including one non-zero exit. **Each of the four sessions stored
  exactly 8 events — no dropped `SessionStart`;** the pre-fix defect did not
  reproduce. All loss counters zero (adapter and project) before and after a
  daemon restart; the 36-id event set is identical across the restart; no
  `faults.log`. 28 hook invocations, `outcomes {success: 28}`, zero unpaired,
  zero errored runs. Every one of the 8 stream `tool_use_id`s has a matching
  stored `tool.finish`. `SessionEnd` and `PostToolUseFailure` both observed live.
- **Stage 4 — supervised desktop pass.** Claude desktop Code 2.1.263,
  project-local `settings.local.json`, isolated home. **A real composer
  submission produced a stored `turn.start`** (native `UserPromptSubmit`, 16:35:35).
  Triggers fired: SessionStart, UserPromptSubmit, PreToolUse/PostToolUse,
  PostToolUseFailure, SubagentStart → `agent.start`, SubagentStop → `agent.end`,
  PreCompact/PostCompact, SessionEnd (on archiving the session — the desktop Code
  tab has no "close"). Esc during an active tool produced no dedicated event
  (`Interrupt` does not exist in the build); the tool call still completed and
  emitted `PostToolUse`. All 6 desktop `tool_use_id`s paired. 38 events, zero
  losses, no `faults.log`. `~/.claude/settings.json` SHA stayed at baseline
  throughout. `/hooks` is terminal-only; trusted-folder `settings.local.json`
  hooks load without it and showed no approval prompt.

Combined live event coverage: **11 of 12 native events** (Stages 1, 3, 4). Only
`Notification` is unobserved live; it is covered by offline contract tests.
`Interrupt` does not exist in this build.

### Gate status — all five criteria met

| # | Criterion | Result |
|---|---|---|
| 1 | Blocking cost p95 ≤ 250 ms, max ≤ 1000 ms | **Met.** Launch benchmark four-caller p95 96.4 ms, max 104.6 ms; interactive `Stop` `durationMs` 51–71 ms corroborates. |
| 2 | Every `tool_use_id` has a stored `tool.finish` | **Met.** Stage 3 CLI 8/8, Stage 4 desktop 6/6. |
| 3 | Real desktop composer submission produces a stored `turn.start` | **Met.** Stage 4, 2026-09-06 16:35:35. |
| 4 | Stored event id set survives a daemon restart, losses zero, faults attributable | **Met.** Stage 3: 36/36 ids, all counters zero, no `faults.log`; `disk::fault` gives per-invocation attribution when a counter is nonzero. |
| 5 | Nothing satisfied by a raised timeout, async backgrounding, or a relaxed deadline | **Met.** `timeout: 2`, exec form, fully synchronous; `outcomes {success: 28}`. |

**WD-022a closes.** Deferred to WD-022b: transcript enrichment, usage
reconciliation, the wider content-capture surface (`error`, `duration_ms`,
`is_interrupt`), and guidance/control delivery. `Notification` live coverage and
macOS/Linux (WD-019) remain open. Pass C (shell attribution against a live
session) was dropped — the installer emits exec form only, so shell startup is
never on Claude's path; the synthetic direct-vs-`bash` comparison in
[evidence](evidence/wd022a-claude-launch.json) attributes the cost.

## WD-024 Rust adapter (closed with external limitations)

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
| [Direct Rust recheck after shared-resolution optimization](evidence/wd024-windows-direct-fast-recheck.json) (20 samples) | 166.4 | 64.6 | 173.0 |
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
The new direct result shows that the shared-resolution optimization benefits the
native adapter too, but direct launch remains a synthetic comparison mode rather
than an observed Codex hook runner.

A later 40-sample Windows PowerShell recheck after the shared-resolution change
improved p95 to 278.7 ms sequential and 563.3 ms for four callers, with all 81
events retained and zero losses. It is materially faster than the original
338.8/833.0 ms measurement, but still misses the 250 ms end-to-end target.

### Console hook-launch canary

2026-09-06: `scripts/codex_hook_canary.py` adds an opt-in canary for the Windows
Codex **CLI** command-launch contract. It writes a randomly named temporary
profile under the normal `CODEX_HOME`, enables only a `SessionStart` hook, and
removes that profile in `finally`. The profile's `commandWindows` invokes
the same PowerShell command form as the production installer and the handler
records only its event name and executable ancestry, never hook input or model
output. A Windows Job Object owns the launched CLI tree and closes it before
the process is collected.

Run it from a standalone PowerShell or `cmd.exe` console, not from a Codex
Desktop terminal:

```powershell
uv run python scripts/codex_hook_canary.py probe --record
```

Save the JSON result, then supply its exact `codex_version` and comma-separated
`process_chain` on subsequent invocations with `--expected-version` and
`--expected-process-chain`. Any version, event, or process-chain difference
fails the command. The canary explicitly refuses a desktop-host environment
because its local `codex.exe` and `cua_node` runtime are shared with the active
application. Two attempted in-host runs produced no marker and therefore did
not establish a baseline; their runtime processes exited without manual
termination. That in-host observation is an execution-host limitation and does
not assess the standalone CMD variation described below.

Standalone CLI baseline, 2026-09-06: `codex-cli 0.153.3` produced
`SessionStart` through `python.exe, python.exe, powershell.exe, codex.exe,
node.exe, cmd.exe, python.exe, python.exe`; the exact expected-value invocation
also passed. The optional `--launcher cmd` variation failed before starting the
handler, while Codex itself exited successfully and reported `SessionStart
Failed`. Thus `commandWindows` is a Windows override, not evidence of a
supported direct-CMD runner selection. Retain the PowerShell launch contract;
do not use the CMD variation as a WD-024 mitigation.

The canary has 20 offline contract tests with the benchmark tests. It verifies
only the console command-launch shape. It neither substitutes for the native
concurrent-delivery/desktop lifecycle matrix nor demonstrates the WD-024
250 ms end-to-end target.

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

WD-024 is closed by explicit user decision with reliable native concurrent delivery,
actual desktop prompt submission/remaining desktop lifecycle triggers, and actual
Codex Windows end-to-end latency recorded as external provider limitations. The
direct Rust target passing is not treated as proof that those gates passed. Future
Windows Codex hook-performance work belongs to WD-026; unavailable lifecycle
triggers remain documented rather than inferred or silently accepted.

The WD-022a live pass exercised the **same binary** on a harness that reports its
own hook lifecycle. After removing the `git rev-parse` subprocess from the
resolution path (shared by both providers), the concurrent-delivery `invalid`
loss **did not reproduce**: four concurrent `claude -p` sessions each stored a
complete event set, all loss counters zero across a daemon restart. Exec-form
launch under four-way concurrency is p95 96.4 ms synthetically (down from
153 ms). Claude Code exposes a per-hook `durationMs` only in interactive
transcripts and only for `Stop`. This addresses WD-024 blocker #2 for the shared
binary; the remaining WD-024 gates (native Codex concurrent delivery, desktop
prompt submission, launch contract) are unchanged and still measured through
Codex. See [WD-022a Claude observation](#wd-022a-claude-observation-gate-met) and
the [per-provider blocker table](../tmp-WD-024.md); what the Claude evidence does
and does not prove about Codex is stated there.

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
