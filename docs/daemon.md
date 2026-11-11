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
Already admitted batches can finish; pending spool files survive for a later start.
`run` runs in the foreground and respects pause; Ctrl+C exits without setting pause.
A crash does not pause collection. `start` and `stop` wait up to ten seconds for
the requested state after acquiring the control lock, returning a nonzero exit
code on timeout. They never signal a PID. `status` returns a JSON snapshot without
starting the core. It also returns an on-demand shared-spool file/byte occupancy
snapshot with its limits; this does not add polling work.
Competing commands use the latest committed desired state.

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
- `data/control.json` is a versioned, atomically replaced single-slot desired-state file:
  a request ID and desired pause state. It is durable across runtime-directory
  cleanup. A later command supersedes the previous request; no unbounded request
  or acknowledgment history is retained.
  This desired-state file and the bounded command request files are controls,
  not event Inboxes; WD-149 leaves their request/acknowledgment mechanism intact.
- `runtime/status.json` contains an instance UUID, diagnostic PID, heartbeat,
  acknowledged request ID, and up to 32 project error IDs. Start acknowledgment is
  published by the core; stop completion is the persistent pause plus released
  ownership. Corrupt or future-version control files are not overwritten.
- `data/policy/socket.json` (WD-022b, schema 2 since WD-140) is the discovery
  file for the **decision channel**: the loopback-only `port`, a per-instance
  random `token`, the adapter's wait budget `timeout_ms` (300), the request
  size budget `max_request_bytes`, and the `subscriptions` the adapter may ask
  about (`{provider, hook_event_name, tool_name?}`). Since WD-142 the daemon
  derives them from the enabled, approved rules and rewrites only the
  `subscriptions` and `max_request_bytes` fields (the latter follows a changed
  `payload_bytes` and also moves the server's read limit, WD-156), at the next
  poll tick, when that set changes (a rule
  approved, edited, enabled or disabled); `[defaults] policy_intervene = false`
  publishes none. The port and token stay valid. With the
  built-ins they are Claude `PreToolUse` on `Agent` and on `Bash`, and
  `PostToolUse` on every tool. It is the same discovery-file pattern as
  `spool/limits.json`. A background thread answers exactly one bounded,
  token-checked request per connection; a bind or publish failure degrades to
  no decision channel at all (a WARNING, not a daemon crash — this optional
  capability must never take down the shared daemon Codex observation also
  depends on). Removed on every exit path, clean or not, so a stale file left
  by an unclean exit is at worst a dead port a later process might reuse,
  caught by the token check.

  **Protocol.** The request is one JSON line,
  `{schema_version: 2, token, provider, event_id, input}`, where `input` is the
  complete hook input and `event_id` is the id the adapter gives the spooled
  hook event. The answer is
  `{schema_version: 2, action, reason?, updated_input?, context?}` with
  `action` one of `allow|deny|ask|rewrite|context|block`; `allow` means "no
  opinion" and the adapter renders nothing for it. `agent_watchdog.rules.engine`
  hosts the rules (declarative TOML rules and the four built-ins, see
  [rules.md](rules.md)); the first non-`allow` action the adapter can render for
  that provider and event wins. A rule whose `log` action matches answers
  `allow` on the wire and is only recorded, and it is recorded only when no
  earlier rule delivered an action. Codex control requires an explicit
  installation-scoped Windows CLI profile and a matching probed cell
  ([WD-151](evidence/wd151.md)); other deployments remain observation-only.
  The answer is `allow` when the project cannot be resolved from `cwd`, and
  while either kill switch (`[defaults] policy_intervene`, or the project's own)
  is off; the global one cannot be switched back on by a project.

  **Fail open.** Anything wrong — unreadable, oversized, or malformed request,
  wrong schema version (a schema 1 adapter against this daemon, or the
  reverse), bad token, unreadable config, a rule that raises — closes the
  connection without an answer, and the adapter reads that as "no opinion".
  A mixed install therefore silently loses the deny rather than ever denying
  by mistake; upgrade the adapter and the daemon together. The request size
  budget is the spool payload limit plus a fixed overhead, so a maximal
  subagent prompt still reaches the rule; it is computed when the daemon starts
  and is not republished on a config change, so raising `payload_bytes`
  needs a daemon restart for the larger requests to be answered.

  **Control events.** Every non-`allow` action the daemon sent is recorded as a
  `control` event (`source = "daemon"`,
  `payload.<provider> = {rule, rule_version, action, reason, text, evidence_ids,
  delivered_at}`, where `evidence_ids` holds the hook event's id, `text` is the
  reason or context the model was sent, and `delivered_at` is when the daemon
  sent it). The daemon cannot see whether the
  adapter rendered the answer: "delivered" means "sent". If the adapter's
  deadline expired first, the event still exists although the model never saw
  the action. If the adapter's spool write then fails (quota or I/O), the
  `evidence_ids` entry points at a hook event that was never stored. The event
  is published as a prepared envelope to the shared schema 2 spool from the
  handler thread (never through a
  `Store`, which the poll thread owns) and recording failures are only logged;
  the answer has already been sent. Like `observation.gap`, a `control` event
  is an ordinary stored event: it counts in per-session and per-project event
  totals, coverage and pipeline telemetry, and (because it is created
  immediately after the hook it answers) moves a session's first or last
  timestamp by no more than that hook's own latency. The findings and reports
  that measure turns, tools, usage and subagents select their event kinds
  explicitly and ignore it.
- `config.toml` is re-read on every poll tick and every policy-socket request
  (no explicit reload command exists or is planned; an edit takes effect on
  the very next tick/request). `agent_watchdog.daemon._ConfigCache` skips the
  parse/validate cost when the file's mtime and size are unchanged since the
  last read, keyed by path (so tests using distinct config files never share
  a stale entry); this only avoids redundant work, it never changes when a
  real edit is picked up.

Status is `paused`, `running`, `unavailable`, or `degraded`. `alive` reports OS lock
ownership, independently of health; PID and heartbeat never establish ownership.
Pause takes precedence. A recent startup failure can therefore be `degraded` with
`alive: false`, including between crash-loop launches. There is no automatic
force-kill of an unresponsive process.

The JSON `reasons` list explains degradation (WD-173):

| Reason | Evidence |
| --- | --- |
| `heartbeat_missing` | A held daemon lock without a published heartbeat. |
| `heartbeat_invalid` | An unreadable status file or an invalid, nonfinite, or future heartbeat. |
| `heartbeat_stale` | A held lock with a heartbeat at least ten seconds old. |
| `startup_failed` | The lock is released after a start that never published its first heartbeat, within the last 60 seconds. |
| `startup_crash_loop` | At least three starts without a first heartbeat within 60 seconds. |
| `backlog_stalled` | A nonempty queue has not shrunk or made drain progress across at least two samples spanning 60 seconds. |
| `storage_lag` | Spool work has waited at least 60 seconds and the newest spool file is at least 60 seconds newer than the newest stored event, or no event has been stored. |
| `project_or_configuration_error` | The heartbeat reports project or configuration errors; `errors` and `transcript_failures` retain the details. |
| `startup_evidence_unavailable` | Durable startup evidence cannot be read or validated. |
| `storage_unavailable` | A store cannot be read within its query budget, or the overall storage comparison deadline leaves projects unchecked. |
| `queue_or_configuration_unavailable` | Queue/configuration inspection failed, or persisted backlog evidence is invalid. |

`health.startup` includes the count, 60-second window and last start time.
`data/daemon-health.json` keeps at most 32 pending starts, independently of the
runtime status file and diagnostic logging level. Only the daemon owner writes it;
the first successfully published heartbeat clears pending starts. Evidence writes
are best effort and never prevent collection. Status inspection does not launch a
daemon or write health evidence. After the startup window expires, a dead daemon
without other degradation evidence returns `unavailable`.

The poll loop samples the shared spool at most once every ten seconds.
`backlog` retains its nonempty queue's file count, first observation
without progress and sample count; `health.stalled_queues` names affected queues.
A smaller queue or a successful drain resets its timer independently of traffic
in other queues. Empty queues never count as stalled. A single status call can
use this existing poll history without waiting or requiring previous status calls.

`health.storage` compares the newest stored `received_at` across registered
projects with spool file modification times, explicitly labeled
`spool_timestamp_source: filesystem_mtime`; these are filesystem observations, not
parsed provider timestamps. New traffic after an idle period gets a 60-second
grace period. SQLite reads use read-only connections, a 100 ms lock timeout and a
100 ms scan budget per project, within a one-second total deadline. `unavailable_projects` names stores that could not be inspected. An unreadable or incomplete comparison is
`unavailable`, never an empty store or confirmed lag. An idle daemon with empty
queues and a current heartbeat stays `running` regardless of its last event's age.

The core disconnects standard streams and uses a detached Windows process or a
new POSIX session. Its working directory is the data directory, so it does not
keep the launching checkout or temporary working directory open. It polls a
bounded window of schema 2 spool records, starting at 250 ms and backing off to
two seconds when idle. Raw hooks are resolved and prepared envelopes validated,
then committed directly through one Store per project per tick. Writers are
reused across that tick's bounded batches; the default batch size of 100 was
selected by the [WD-149 measurement](evidence/wd149.md). Queue files are removed only after commit.
Idle project visits back off exponentially from one second to 60 seconds; a committed spool record wakes its project for the next tick. Transcript progress, session refresh and pending maintenance keep the project active, while prior project failures remain visible between visits. Source listings omit reader tail and counter blobs; those are loaded only when a source needs processing.

One cooperative 200 ms maintenance allowance is shared across project writers;
round-robin scheduling and phase/cursor state preserve progress across Store
opens. Pending maintenance or temporary quota pressure defers queued work without
counting a loss. WAL and synchronous FULL durability remain unchanged.
One unavailable project is reported and retried without blocking other projects.
Configuration is reread each iteration. Status snapshots replace one file instead
of producing an unbounded process log. Large project counts and idle overhead
validation remains WD-024 after the WD-008 Python baseline exceeded the latency target. WD-007 [storage policy](storage.md) enforces quotas and
retention and exposes persistent rejection counters in `daemon status`.

The Python core also keeps a bounded, best-effort diagnostic log at
`data/watchdog.log`. It rotates to numbered archives according to global
`[defaults]` `log_files` and `log_bytes`; `log_level = "INFO"` excludes normal
hot-path DEBUG records. Entries contain only fixed decision codes, a shape-bounded
`error_type`/`field`, bounded numeric metadata, and local Watchdog project/event
UUIDs. By default they never contain paths, prompts, provider/session IDs,
commands, tool output, exception messages, or tracebacks. Setting `[defaults]`
`log_detail = true` adds a de-identified, 200-character `detail="..."` error
string (paths kept, credentials stripped) for local debugging; see
[storage](storage.md#daemon-diagnostic-log). A logging failure never affects
daemon or hook behavior.

A checkout whose state cannot be fingerprinted within the bounds is logged once per
checkout and reason as `event=checkout decision=unavailable error_type=<reason>`; see
[storage](storage.md#checkout-fingerprint-coverage-wd-121).

Transcript-enrichment diagnostics use a fixed reader-specific `error_type`
allowlist rather than exception class names. Newly active failures are logged once
per project/code pair. A reader failure holds a project `degraded` only while its
source has been referenced by a hook within `transcript_failure_minutes` (15 by
default); older reader gaps remain diagnostic history. Status includes the active
failure codes per project without paths or session identities. With the default
`log_detail = false` these records contain no
transcript content or exception text. Enabling `log_detail` applies the same
bounded, credential-redacted local-detail policy as other caught exceptions.

## Python entry points

`mutate_registry(paths, callback)` loads the current registry under the control
lock, applies the callback, and atomically saves it. The callback must not acquire
that lock again. Existing low-level `save_config` remains a caller-coordinated API.

`publish(paths, envelope)` checks pause and the registered project ID under the
same lock, publishes the event with the configured payload cap, and ensures a
daemon launch without waiting for readiness. It returns false for paused or
unregistered input. `start(paths, explicit=False)` ensures crash recovery without
clearing pause. Errors propagate to the fail-open hook adapter; these are
internal library entry points, not a provider hook protocol. Prepared envelopes
join native raw-hook records in the same schema 2 spool; there is no runtime
Inbox API or legacy queue reader. Both paths use the ordinary daemon-start and
readiness mechanism. Ingest preserves raw content subject to capture and limits.

## Synthetic HTTP transport experiment

The `--benchmark-http` flag on `scripts/benchmark_hooks.py` opts into a direct
loopback HTTP experiment in scratch state. It requires a native
`--adapter-executable` and a subscribed synthetic `--event` such as `PreToolUse`.
The script passes `WATCHDOG_BENCHMARK_HTTP=1` only to its child
daemon and hooks. The daemon publishes `policy/benchmark-http.json` for that
lifetime and removes it on exit. HTTP and the installed TCP path invoke the
same request authentication, schema checks, evaluator, session tracker, response
rendering, and control recording; HTTP adds no forwarding socket. Native hooks
retain their request deadline, fail-open behavior, and unconditional spooling.
The installer and user configuration expose no HTTP transport setting. Benchmark
outputs describe synthetic process latency, decision requests, errors, and
delivery completeness; they are not live-provider acceptance evidence.

## Windows lifecycle evidence

2026-09-05, CPython 3.12.13, Codex CLI 0.153.4:

| Surface | Observed boundary | Result |
|---|---|---|
| Codex CLI | `codex exec` launched the isolated daemon through a shell tool, completed its turn, and exited with code 0 | Same daemon instance and advancing heartbeat after CLI exit |
| Local desktop coding | Existing probe task launched the daemon through a shell tool, completed, and was archived; app task status became `notLoaded` | Same daemon instance kept its heartbeat and committed a new manually published synthetic event after task unload |
| Claude CLI (WD-022b, 2026-09-26) | A real interactive `claude` session ran a full task (subagent spawns, `/compact`) then exited via `/exit` | Same daemon instance (pid unchanged) stayed `alive: true`; the session's own `SessionEnd` was admitted and committed *after* the CLI process had already exited (event count rose from 84 to 87 between the pre- and post-exit checks) |
| Claude desktop Code (WD-022b, 2026-09-26) | A real composer submission produced a stored `turn.start`; the task was then archived (no "close" exists) | Same daemon instance (same pid as the CLI check above, one continuous isolated run) stayed `alive: true` through and after the archive |

Both isolated daemons were stopped through their control files and both test
tasks were archived. An attempted extra CLI event probe hit Git discovery failure
inside a nested scratch directory and stopped that daemon in cleanup; it does not
count as post-exit ingestion evidence. The desktop event probe used a separate
temporary project and passed. No real transcript content was collected. The
WD-022b Claude rows above are real `claude -p`/desktop sessions on a fresh
scratch repository (full account: [verification.md](verification.md#wd-022b-live-verification-trustreload-capability-gaps-process-lifecycle)),
not the generic `detach_probe.py --claude-init` synthetic probe (WD-018),
which was previously inconclusive inside a restricted execution environment.

These checks establish survival of the tested CLI exit and desktop session unload, now including Claude alongside Codex.
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
