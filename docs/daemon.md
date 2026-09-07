# Local daemon

WD-005 provides a polling core and lifecycle commands. The WD-006 Codex hook adapter
and installer are described in [hooks.md](hooks.md); project CLI commands remain
WD-008. Daemon commands do not change hook configuration.

```console
agent-watchdog daemon start
agent-watchdog daemon status
agent-watchdog daemon stop
agent-watchdog daemon pause
agent-watchdog daemon run
```

`start` clears persistent pause and starts a detached process when necessary.
`stop` and its synonym `pause` persist pause and wait for cooperative shutdown.
Already admitted batches can finish; pending inbox files survive for a later start.
`run` runs in the foreground and respects pause; Ctrl+C exits without setting pause.
A crash does not pause collection. `start` and `stop` wait up to ten seconds for
the requested state after acquiring the control lock, returning a nonzero exit
code on timeout. They never signal a PID. `status` returns a JSON snapshot without
starting the core. Competing commands use the latest committed desired state.

Use `--home <directory>` before `daemon` to isolate configuration, data, and runtime
for a manual experiment. Individual `--config <file>`, `--data <directory>`, and
`--runtime <directory>` options override those locations. Use consistent locations
across callers; the resolved data directory defines the daemon's identity.
Without overrides, locations come from platformdirs. No login service is installed.

## Ownership and controls

- `data/daemon.lock` is an OS-backed lock held for the process lifetime. Lock files
  are never removed. Concurrent launch contenders exit when the lock is held.
- Atomic file replacement retries Windows sharing/access errors for at most
  100 ms, since a concurrent reader can briefly prevent rename. Persistent errors
  still fail with the original destination preserved and temporary files cleaned.
- `data/control.lock` serializes load/edit/save registry mutations, desired-state
  changes, and event admission. Ordinary callers wait at most five seconds for
  this lock; adapter entry points wait at most 100 ms. This is a lock-wait bound,
  not a measured whole-hook latency guarantee.
- `data/control.json` is a versioned, atomically replaced single-slot control inbox:
  a request ID and desired pause state. It is durable across runtime-directory
  cleanup. A later command supersedes the previous request; no unbounded request
  or acknowledgment history is retained.
- `runtime/status.json` contains an instance UUID, diagnostic PID, heartbeat,
  acknowledged request ID, and up to 32 project error IDs. Start acknowledgment is
  published by the core; stop completion is the persistent pause plus released
  ownership. Corrupt or future-version control files are not overwritten.

Status is `paused`, `running`, `unavailable`, or `degraded`. A held lock with a
missing/invalid heartbeat, a heartbeat older than ten seconds, or project/config
errors is degraded. Pause takes precedence; `alive` separately indicates whether
shutdown is still pending. PID and heartbeat never establish ownership by
themselves. There is no automatic force-kill of an unresponsive process.

The core disconnects standard streams and uses a detached Windows process or a
new POSIX session. Its working directory is the data directory, so it does not
keep the launching checkout or temporary working directory open. It polls in
batches of at most 100 deliveries per registered project, starting at 250 ms and
backing off to two seconds when idle. Project writers close between batches.
One unavailable project is reported and retried without blocking other projects.
Configuration is reread each iteration. Status snapshots replace one file instead
of producing an unbounded process log. Large project counts and idle overhead
validation remains WD-024 after the WD-008 Python baseline exceeded the latency target. WD-007 [storage policy](storage.md) enforces quotas and
retention and exposes persistent rejection counters in `daemon status`.

The Python core also keeps a bounded, best-effort diagnostic log at
`data/watchdog.log`. It rotates to numbered archives according to global
`[defaults]` `log_files` and `log_bytes`; `log_level = "INFO"` excludes normal
hot-path DEBUG records. Entries contain only fixed decision codes, bounded numeric
metadata, and local Watchdog project/event UUIDs. They never contain paths,
prompts, provider/session IDs, commands, tool output, exception messages, or
tracebacks; a logging failure never affects daemon or hook behavior.

## Python entry points

`mutate_registry(paths, callback)` loads the current registry under the control
lock, applies the callback, and atomically saves it. The callback must not acquire
that lock again. Existing low-level `save_config` remains a caller-coordinated API.

`enqueue(paths, envelope)` checks pause and the registered project ID under the
same lock, publishes the event with the configured payload cap, and ensures a
daemon launch without waiting for readiness. It returns false for paused or
unregistered input. `start(paths, explicit=False)` ensures crash recovery without
clearing pause. Errors propagate to the fail-open hook adapter; these are
internal library entry points, not a provider hook protocol. They do not redact
content yet. Low-level `Inbox.publish` deliberately bypasses admission controls
and is reserved for storage tests and controlled replay.

## Windows lifecycle evidence

2026-09-05, CPython 3.12.13, Codex CLI 0.153.4:

| Surface | Observed boundary | Result |
|---|---|---|
| Codex CLI | `codex exec` launched the isolated daemon through a shell tool, completed its turn, and exited with code 0 | Same daemon instance and advancing heartbeat after CLI exit |
| Local desktop coding | Existing probe task launched the daemon through a shell tool, completed, and was archived; app task status became `notLoaded` | Same daemon instance kept its heartbeat and committed a new manually published synthetic event after task unload |

Both isolated daemons were stopped through their control files and both test
tasks were archived. An attempted extra CLI event probe hit Git discovery failure
inside a nested scratch directory and stopped that daemon in cleanup; it does not
count as post-exit ingestion evidence. The desktop event probe used a separate
temporary project and passed. No real transcript content was collected.

These checks establish survival of the tested CLI exit and desktop session unload.
The user explicitly accepted desktop session unload as sufficient for WD-005.
They do not establish survival of closing the entire desktop application, logout,
or every possible Windows job policy. Launch from native hooks and their trust
boundary are covered separately by [WD-006](hooks.md). macOS/Linux host verification remains WD-019; it was not
performed here. In environments that prohibit detachment, explicitly starting
the core from an independent user terminal is the manual fallback; automatic
service installation is outside this milestone.

The CLI invocation syntax was checked against installed `codex exec --help` and
the [official developer command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli).
Lifecycle conclusions above come from the live probes, not documentation claims.
