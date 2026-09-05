# Observation CLI

Commands emit JSON. Put `--home DIR` (isolated state) or `--config`, `--data`, and
`--runtime` before the command. Without overrides, platformdirs supplies the
user-local paths. Read commands do not start collection or migrate databases.

## Projects

```console
agent-watchdog project add /path/to/repository
agent-watchdog project list
agent-watchdog project relocate PROJECT_UUID /new/path
agent-watchdog project remove PROJECT_UUID
```

`add` prints the registered project, including its UUID. Registration is explicit;
Git worktrees share a project through the common Git directory, while clones stay
separate. Mutations use the daemon's registry lock. Relocation requires the old
root to be absent and preserves UUID, overrides, and collected data. Removal only
unregisters the project; it does not delete its database or uninstall hooks.

Use global defaults or project `overrides` in config.toml for capture/retention
settings, as described in [contracts](contracts.md). Hook installation remains an
explicit [separate operation](hooks.md); registration does not grant native trust.

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
agent-watchdog sessions list --project PROJECT_UUID
agent-watchdog sessions show SESSION_ID --project PROJECT_UUID --limit 100 --offset 0
agent-watchdog sessions show --unassigned --project PROJECT_UUID
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

Exit codes: 0 for successful inspection/mutation; 1 for errors or unhealthy doctor
results; 2 for invalid command syntax; 130 for interruption. Observation hooks keep
their separate fail-open zero-exit contract.

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

Latency includes process startup, Python imports, Git resolution, and admission
with an already running daemon. It excludes native shell/provider overhead and
does not measure cold daemon startup. Percentiles use nearest rank. On Windows,
idle CPU is the daemon's process CPU-time delta and memory is its working set;
other hosts report those fields unavailable until WD-019 verification.

The initial Windows run measured p95 1089.5 ms sequentially and 1505.7 ms with four
concurrent callers, above the 250 ms target. All 81 events survived restart with
zero recorded losses. Idle CPU consumed 0.219 seconds over 20.609 seconds and the
working set was about 32.8 MiB. Import profiling isolated Python/Pydantic startup
as the dominant cost. The user selected a small Rust adapter in WD-024, before
WD-009, retaining the Python core and inbox contract. These baseline numbers do
not claim the latency acceptance target has passed.
