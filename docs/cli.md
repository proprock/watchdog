# Observation CLI

Commands emit JSON. Put `--home DIR` (isolated state) or `--config`, `--data`, and
`--runtime` before the command. Without overrides, platformdirs supplies the
user-local paths. Read commands do not start collection or migrate databases.

## Projects

```console
agent-watchdog project add /path/to/repository
agent-watchdog project list
agent-watchdog project relocate PROJECT /new/path
agent-watchdog project remove PROJECT
```

`add` and `list` print a short `project` alias derived from the repository folder
name. The first exact `repo` keeps that name; later exact collisions become
`repo-2`, `repo-3`, and so on, in registration order while avoiding every existing
alias. Case is preserved and significant: `Project` and `project` select different
projects. Aliases are recomputed after add, removal, or relocation, so query
`project list` again after a registry change. All project-selection arguments accept
an alias; UUID input remains accepted for compatibility but is not emitted in normal
CLI responses.

Registration is explicit; Git worktrees share a project through the common Git
directory, while clones stay separate. Mutations use the daemon's registry lock.
Relocation requires the old root to be absent and preserves UUID, overrides, and
collected data. Removal only unregisters the project; it does not delete its
database or uninstall hooks. UUIDs remain internal storage/control identifiers and
appear only in raw retained envelopes and offline export artifacts.

Use global defaults or project `overrides` in config.toml for capture/retention
settings, as described in [contracts](contracts.md). Hook installation remains an
explicit [separate operation](hooks.md); registration does not grant native trust.

## Summary

```console
agent-watchdog summary
```

Returns a read-only JSON row for every registered project with its alias, root,
database state, session count, and event count, plus known cross-project totals.
`session_count` groups distinct non-null provider/session-ID pairs; unassigned
events remain in `event_count` but do not create a session. The top-level observed
totals include only ready databases. If a project has no database or an inspection
error, its counts are null and `complete` is false rather than treating unavailable
data as zero. Database errors also set `ok` to false and return exit code 1.

Each ready project is read through its own short-lived SQLite read-only snapshot.
Concurrent ingestion can therefore change one project between snapshots; the
cross-project totals are known observations, not a global transaction.

## Pipeline telemetry

```console
agent-watchdog telemetry --project PROJECT --since 2026-09-08T00:00:00+00:00
```

`telemetry` reads persisted delivery-stage timestamps and bounded queue samples
without starting collection or modifying stored observations. It reports coverage,
throughput and peak rate, stage-delay percentiles, observed queue occupancy, and
loss counters. If daemon-wide `pipeline_telemetry` is disabled, it returns an
explicit disabled result rather than treating missing measurements as zero.

## Token and process telemetry

```console
agent-watchdog usage --project PROJECT
agent-watchdog usage --project PROJECT --since 2026-09-08T00:00:00+00:00 --until 2026-09-09T00:00:00+00:00
```

`usage` aggregates the schema-v6 `event_facts` projections from a read-only
snapshot. It reports raw token sums grouped by provider and, under `token_by`, by
model, reasoning effort, attribution state, conversation, turn, and day; a
`process` block with per-`tool_name` cost and error rate, tools and tool
wall-time per turn, permission-mode throughput, permission-prompt count,
inter-turn latency, and subagent cost by `agent_type`; and a `coverage` block
with the non-NULL count per projected column, kept separate from the aggregates.
Each token counter is summed as reported; `total_tokens` stays absent for Claude
rather than being reconstructed from the parts. `--since`/`--until` bound
`received_at`. A database older than schema v6 is an explicit error, not an empty
result.

With `--tariffs PATH` (or `[pricing] tariffs` in `config.toml`), `usage` adds a
`pricing` block: a **list-price estimate, not billed spend**. Each `usage` row is
priced from its own `model` and timestamp against the latest tariff section
whose ISO date is at or before it, then grouped by provider, model, and day. The
raw token blocks are untouched; the estimate is recomputed on every call, so
editing the tariff file re-prices history. Provenance is explicit: per-group
`cost_estimate` (a decimal string), a `by_field` split
(`input`/`output`/`cache_read`/`cache_write`), `unpriced_tokens` with
`unpriced_reasons` (`model_not_in_tariff`, `before_earliest_tariff`,
`rate_field_missing`), and the `tariff_dates` applied. `reasoning_output_tokens`
and `total_tokens` are never priced. A missing or malformed tariff file is an
error. See [`pricing.example.toml`](../pricing.example.toml) for the format.

## Doctor

```console
agent-watchdog doctor
```

Reports registration roots, database availability/schema/event count, disk usage,
remaining admission capacity, loss counters, and daemon state. A missing database
means collection is unavailable, not corrupt. Corrupt, wrong-project, and future
databases are errors. An unregistered fresh installation does not create state.
Existing daemon lock files may be probed for process ownership. Doctor does not
repair files, inspect native trust, or infer event coverage from hook definitions.

## Sessions

```console
agent-watchdog sessions list --project PROJECT
agent-watchdog sessions show SESSION_ID --project PROJECT --limit 100 --offset 0
agent-watchdog sessions show --unassigned --project PROJECT
```

Without `--project`, the current directory must resolve to a registered project.
List groups by provider and native session ID, newest received time first. Events
without a session ID form an explicitly unassigned group. Summary counts describe
observed records, not tasks completed. Missing session start/end and unenriched
usage are explicit gaps. Stop does not establish session exit or task success.

Show returns stored envelopes in insertion order. It defaults to provider `codex`;
`--provider` selects another recorded namespace without conflating identical IDs.
Content appears only as retained by the storage policy. An unknown session is an
error; an offset beyond an existing session returns an empty page.

Both list and show support `--limit 1..1000` and nonnegative `--offset`. `has_more`
indicates another page. Each call uses a read-only SQLite snapshot and closes it
promptly so maintenance can reclaim WAL space. Pages from separate calls are not
a single snapshot if concurrent ingestion changes the data.

## Shadow report

```console
agent-watchdog report --project PROJECT --session SESSION_ID
agent-watchdog report --project PROJECT --provider codex
```

`report` reads one provider namespace from a SQLite snapshot and makes no control
request, network call, or worktree mutation. It returns a timeline, wall/active-time
coverage, tool outcome/output-size metrics, compaction and usage observations, and
versioned shadow findings. A finding contains rule version, evidence event IDs, count,
and an explicit attribution state. The v1 rules require three matching observations:
same tool input/outcome, same structured error, or the same pytest/JUnit failing set
for a matching test command. Git A-to-B-to-A fingerprints are reported as an
uncertain-attribution signal only. Missing lifecycle/usage/output data remains a gap;
waiting, exit code zero, and session Stop do not establish a stall, progress, or task
success.

## Labels, pins, export, and purge

```console
agent-watchdog label SESSION_ID --project PROJECT --outcome success --task-type bugfix
agent-watchdog label SESSION_ID --project PROJECT --outcome partial --progress stuck
agent-watchdog verdict --project PROJECT --session SESSION_ID --rule repeated_tool_outcome   --rule-version wd-010.v1 --fingerprint FINGERPRINT --verdict true_positive
agent-watchdog verdict --project PROJECT --checkout CHECKOUT_ID --rule diff_oscillation   --rule-version wd-010.v1 --fingerprint FINGERPRINT --verdict uncertain
agent-watchdog pin SESSION_ID --project PROJECT
agent-watchdog pin SESSION_ID --project PROJECT --unpin
agent-watchdog export --project PROJECT --session SESSION_ID --output review-bundle
agent-watchdog purge SESSION_ID --project PROJECT
```

`label`, `verdict`, `pin`, and `purge` submit bounded requests to the running
core and wait for its durable acknowledgement; they do not write SQLite from the
CLI. Labels are provider-scoped and store one outcome (`success`, `partial`,
`failed`, `abandoned`, or `unknown`) plus an optional free-form task type. Pin
protection uses the existing session-retention policy; identical native session
IDs across providers remain separate for selection and labels.

`label` also accepts `--progress` (`progress`, `slow`, `stuck`, or
`externally_blocked`) and a free-form `--note`. Both are manual review fields;
omitting either keeps whatever a previous review recorded, so correcting an
outcome never discards a progress judgement.

`verdict` records one manual correctness judgement for a shadow finding:
`true_positive`, `false_positive`, or `uncertain`. Findings are recomputed on
every read and carry no stored identity, so a verdict is keyed by the rule, its
version, and the `fingerprint` that `report` prints for each finding. Use
`--session` for a session-scoped rule and `--checkout` for `diff_oscillation`,
whose evidence is a checkout's diff history rather than one session's events.

`export` is an offline, read-only snapshot. It requires one or more repeated
`--session` values and writes a new directory containing `events.jsonl`,
`summary.md`, `manifest.json`, and `manual-prompt.md`. The manifest records the
format version, selected labels, counts, gaps, and the recommended content review.
The export never calls an LLM or network service, and it refuses to
overwrite an existing directory. A content review before sharing the bundle
externally is recommended, not enforced; traces and outputs are untrusted data,
not instructions.

`purge` permanently removes only Watchdog-owned rows, retained artifacts, labels,
finding verdicts,
pins that no longer protect another provider record with the same native session
ID, and Watchdog's transcript-reader state for the selected provider/session. It
does not inspect, modify, or delete vendor transcripts or project files.

Exit codes: 0 for successful inspection/mutation; 1 for errors or unhealthy doctor
results; 2 for invalid command syntax; 130 for interruption. Observation hooks keep
their separate fail-open zero-exit contract.

## Calibrating the shadow rules

```console
uv run python scripts/calibrate.py sample   --project PROJECT
uv run python scripts/calibrate.py annotate --project PROJECT
uv run python scripts/calibrate.py report   --project PROJECT
```

This opt-in script supports the WD-012 calibration exercise. It never invokes a
provider and never changes harness behaviour.

`sample` reads the store read-only and freezes a cohort in
`docs/evidence/wd012-sample.json`, with a flat `.csv` view beside it for sorting
and eyeballing. It keeps every session that produced a finding and adds a seeded
draw from sessions that produced none, so false negatives stay countable. Only
settled sessions qualify: a session with an observed `session.end`, or whose last
event is older than `--settle-hours` (default 2). `--min-events` (default 20)
drops start/end-only noise. The manifest records the query, seed, strata, and
skip counts, and stores each session's findings with their fingerprints, because
an open session can grow a finding group and change its identity. It contains no
prompt or output text.

`annotate` walks the frozen cohort and resumes at the first session that carries
no label. Each card shows its counts, its first and last prompt, the last
assistant message, the tool inputs it repeated, the last few timeline rows, its
findings, and its current labels, because counts alone do not show whether a
session was stuck. A `content` line reports how much was stored per kind
(`prompts 6/6 | replies 5/6, 1 not stored`), so an empty answer from the agent
stays distinct from an answer Watchdog never captured, and a `results` line
counts how the tool calls ended, including calls whose result was never
observed. A failure is what the provider signalled, never the string
`PostToolUseFailure` appearing in captured output; where the frozen sample's
count disagrees with the observed one, the card says so. Content comes from the
store and is shown only on screen.

The findings on the card are the frozen sample's, and verdicts attach to those
fingerprints. The card also re-analyses the session and says whether a live
re-analysis would now produce a different set, because a report run today can
count findings the frozen cohort does not contain.

`o`, `t`, `p`, and `r` set outcome, task type, progress state, and a reviewer
note from a numbered menu, and a digit records a verdict for the numbered
finding, after printing that finding's evidence as readable timeline rows rather
than event identifiers. `l` prints the turn/tool timeline, where a tool start and
its finish collapse into one row with the command, outcome, exit code, duration,
and the text the tool returned (`l 50` or `l all` widen it); `usage` events are
left out because token counters show no work to judge. A provider that reports no
exit code leaves the outcome unclassified, so the row shows the returned text
rather than a guessed outcome, and an output field that is present but empty
reads as `no output`. `d` prints the live re-analysis in
readable form and labels it live; `dj` still prints the raw report JSON. `x`
prints the observed vendor transcript path, the full record behind the triage,
and offers to open it with the system viewer: the path comes from an untrusted
trace, so only an existing `.jsonl`, `.json`, `.log`, `.md`, or `.txt` file is
handed over and Watchdog never writes to it. `c` reviews the checkout-scoped
findings once rather than once per session. Every answer is sent to the running
core and the acknowledgement is printed; a rejected write is reported and
nothing advances. Rerunning resumes from what the store already holds.

The labelling rules themselves - what each progress state, outcome, and verdict
means, and what a reviewer refuses to decide - are recorded in
[wd012-protocol.md](evidence/wd012-protocol.md).

`report` joins the frozen cohort with the recorded annotations and writes
`docs/evidence/wd012-calibration.json` plus a markdown report. Precision is
reported per rule with its denominator, `uncertain` is kept separate from both
sides, and a rule with no observation is `null` with a stated reason rather than
100%. Hook overhead comes from the delivery trace already stored on envelopes;
transcript-sourced events carry none, so the denominator is disclosed.

## Measuring overhead

```console
uv run python scripts/benchmark_hooks.py --output docs/evidence/wd008-windows-baseline.json
```

This opt-in script creates an isolated Git repository/worktree and invokes the
Watchdog hook with synthetic inputs. It does not install hooks or call a provider.
By default it measures 40 sequential and 40 four-way concurrent subprocesses,
plus 20 seconds idle. It verifies two session identities, all 81 events, and
restart persistence, then stops its owned daemon and removes temporary state.
`--samples` requires at least 20; `--idle-seconds` accepts 1..60.

Select a built or installed native binary with `--adapter-executable PATH`.
On Windows, `--shell powershell.exe` or `--shell pwsh.exe` includes the generated
`commandWindows` invocation with `-NoLogo -NoProfile -NonInteractive -Command`.
The default `--shell direct` excludes the shell. For example:

```console
uv run python scripts/benchmark_hooks.py --adapter-executable native/target/release/agent-watchdog-hook.exe --shell pwsh.exe --output shell-benchmark.json
```

The benchmark waits for a healthy daemon/PID before measuring. Each subprocess
has a 15-second diagnostic timeout, unlike the installed hook's two-second
timeout. The first call is reported separately from the percentile samples;
neither warmed percentiles nor successful delivery under 15 seconds establishes
that the first native hook will finish within two seconds. Reports identify the
launch mode and remain synthetic, even when the generated shell command is used.

Latency includes process startup, adapter runtime, Git resolution, and admission
with an already running daemon, plus the shell when explicitly selected. It
excludes provider scheduling and does not measure cold daemon startup.
Percentiles use nearest rank. On Windows,
idle CPU is the daemon's process CPU-time delta and memory is its working set;
other hosts report those fields unavailable until WD-019 verification.

The initial Windows run measured p95 1089.5 ms sequentially and 1505.7 ms with four
concurrent callers, above the 250 ms target. All 81 events survived restart with
zero recorded losses. Idle CPU consumed 0.219 seconds over 20.609 seconds and the
working set was about 32.8 MiB. Import profiling isolated Python/Pydantic startup
as the dominant cost. The user selected a small Rust adapter in WD-024, before
WD-009, retaining the Python core and inbox contract. These baseline numbers do
not claim the latency acceptance target has passed.
