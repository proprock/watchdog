# Architecture and contracts

Status: accepted design decisions, not an implementation. Date: 2026-09-05.

## Boundaries

A local single-user system, without a network service, centralized database, container infrastructure, or custom agent harness. Two provider adapters serve CLI/desktop; the surface is an observation attribute, not a separate core. If the surface cannot be reliably identified, record `unknown`.

```text
Codex hooks -- adapter --+
                        +-- atomic file inbox -- single user core
Claude hooks - adapter -+                        +-- project A / SQLite + artifacts
                                                 +-- project B / SQLite + artifacts
transcripts (optional enrichment) ----------------+
CLI -- project selection -- read-only queries / queued mutations
```

## Processes and delivery

- `agent-watchdog hook codex|claude` reads JSON stdin, checks the registered project, limits size, redacts known secrets, and atomically places an envelope in the project inbox using a temporary file and rename on the same disk. Do not run Git diff, analysis, or LLM calls inside a hook. Stdout is empty; expected errors exit with code 0. Diagnostics are local and size-limited.
- Create the envelope ID before writing and preserve it during replay. Reprocessing an envelope is idempotent. Deduplicate repeated provider events only when a stable native ID is available, not merely by text hash.
- The hook ensures a detached `agent-watchdog daemon run` starts if the core is unavailable. POSIX: a new session; Windows: a detached process without a window. Do not inherit the harness stdin/stdout/stderr handles.
- One OS-backed lock per user, held for the core's lifetime. Competing starts fail to acquire the lock and exit. PID/heartbeat are diagnostics, not the sole exclusivity mechanism. Never kill an unrelated process after PID reuse.
- The core lives until logout/stop; the next hook recovers a crash. This does not guarantee continuous operation without new events. Verification on each OS is mandatory: some process containers/job objects may restrict detachment. Provide explicit user autostart as a fallback in those environments.
- `daemon stop` sets a persistent pause and stops the core through the control inbox; new hooks neither start it nor accumulate events until `daemon start`. A crash does not set pause. `daemon status` distinguishes paused, running, unavailable, and degraded.
- The core commits events sequentially into each project's SQLite database; delete inbox entries only after commit. A crash between commit and deletion is safe on replay. Put malformed records in bounded quarantine without blocking the entire queue.
- Poll inboxes every 250 ms while active, backing off to 2 s when idle. Store event time and received time separately; do not promise global ordering across processes. Late events recompute affected aggregates.
- Hooks do not wait for analysis or core readiness. Target full-call p95 latency of at most 250 ms on the measured machine, with a 2 s hook timeout. Measure Python startup and writing separately; do not hide their cost through async configuration.

## Projects and storage

Resolve user config/data/runtime directories with platformdirs. User TOML contains the registered project list and defaults. `project add <path>` explicitly registers a UUID, canonical root, and Git common-dir; worktrees resolve to that UUID. A separate clone gets a separate UUID. Identify non-Git projects by canonical root. Moving a project requires explicit `project relocate`, not merging by remote URL.

One directory per UUID: SQLite (WAL), inbox, artifacts, and quarantine. The registry contains no aggregate analytics. Store large raw outputs as separate redacted artifacts; the database holds references, fingerprints, and bounded excerpts. Do not read arbitrary paths supplied by hooks: transcripts must be within known provider directories and associated with the observed session. Do not recursively scan all user files.

The core is the only database writer. The CLI uses read-only connections; labels, pin, and purge pass through the control inbox with a request ID and acknowledgment. Read-only reports work while the core is stopped. Require a schema version and sequential migrations; reject unsupported newer schemas with a clear error and no overwrite.

Defaults are configurable in user TOML, with overrides by project UUID:

| Setting | Value |
|---|---:|
| Content / aggregates and labels | 30 / 180 days |
| Total project quota, including WAL/inbox/artifacts | 2 GiB |
| Inbox within the total quota | 64 MiB |
| Maximum persisted event payload | 1 MiB |
| Process logs | 5 files of 10 MiB each |

Delete expired content first, then old unpinned content, then old unpinned sessions. Pin protects against deletion, not quota accounting: if space cannot be reclaimed, stop accepting content/events and report degraded status with loss counters. Never silently delete pinned data. Reserve space for checkpoint/diagnostics; bound concurrent inbox writes with a short project lock. Account for auxiliary files; SQLite cleanup must reclaim disk space using planned incremental vacuum/checkpoint support. Never delete existing vendor transcripts.

## Normalized events

Versioned envelope: `schema_version`, `event_id`, `provider`, `provider_version` (nullable), `surface`, `project_id`, `checkout_id`, `session_id`, `agent_id`, `parent_agent_id`, `turn_id`, `native_event_id`, `kind`, `occurred_at`, `received_at`, `source`, `payload`, and `availability`. Unavailable identifiers are nullable; do not invent them. Distinguish root sessions from subagents; leave unresolved ownership unassigned instead of mixing counters.

Kinds: session/turn lifecycle, tool start/finish, compaction, subagent lifecycle, waiting/interrupt, usage, and observation gap. Allow provider-specific payloads in a namespaced field. Preserve unsupported versions/events as unknown with a bounded payload. CLI JSON export uses the same versioned envelope; this is the first external data interface.

Hooks are the primary source. Transcript enrichment is separate, with persisted offsets and handling for partial lines, truncation, rotation, and resume. Bind reader formats to verified fixtures/versions. On format changes, disable only enrichment and report a gap. Correlate by native IDs; do not sum identical usage snapshots. Distinguish cumulative counters from per-turn deltas. Input/output/cached tokens are nullable; keep estimated and measured values separate. Subscription cost cannot be derived from token counts; defer monetary estimates.

## Analysis and reports

- Distinguish turn completion, session stop, and verified task outcome. Users label outcomes: success, partial, failed, abandoned, unknown. Task type is a free-form label with suggested bugfix/feature/refactor/docs/research values.
- M2 rules: repeated command+outcome, identical error fingerprint, repeated failing-test set from comparable runs, compactions, and output size. Preserve rule versions, evidence IDs, counts, and explanations. An initial repetition threshold of 3 is a shadow-analysis hypothesis, not an intervention command.
- Do not normalize commands aggressively: `pytest -x` changes the observed failure set. Compare test deltas only with matching target/configuration and completed results. The first structured parser covers pytest/JUnit; other outputs provide best-effort signals, not proof of acceptance.
- Compute debounced Git diff fingerprints per checkout in the core; concurrent agents/user edits make attribution uncertain. A->B->A is a signal, not proof of a stall. Snapshots must not modify the index or worktree.
- No universal no-progress score based on missing events. Distinguish observed, inferred, and unknown for every finding; waiting for a user/tool or incomplete data is not a stall. Exit code 0 does not automatically reset signals.
- CLI reports show timeline, coverage/gaps, active time separately from wall time, repetitions, available usage/compaction, and labels. Without evidence of interval start/end, active time is unknown.
- Export selected sessions as a Markdown summary, JSONL events, a manifest with versions/completeness/redaction, optional redacted artifacts, and a prompt template for manual LLM analysis. No automatic transmission. Clearly mark trace content as untrusted data.

## Later extensions

Keep intervention separate from observations: proposal, evidence, required capability, expiry, and delivery receipt. M0-M2 do not implement placeholders for it. Begin with shadow calibration, then opt-in guidance at a native safe boundary. Stop continuation is not a hard pause. Do not assume App Server can attach to arbitrary desktop sessions: investigate session ownership/attach separately first.
