# Architecture and contracts

Status: accepted design decisions. WD-003 [contracts](contracts.md), WD-004/007 [storage, retention, and redaction](storage.md), WD-005 [daemon](daemon.md), and WD-006 [hooks](hooks.md) are implemented. WD-008 provides the [observation CLI](cli.md); WD-009 adds Codex rollout-v1 usage enrichment; WD-010 adds deterministic, read-only shadow analysis and reports. WD-024 replaced the slow Python adapter with Rust, retaining the Python core and inbox contract. WD-027 made that adapter spool-and-forget: it redacts and durably spools each event, and the daemon resolves the checkout, builds the envelope, and admits it. WD-115 is pending: it moves the credential filter off the ingest path to the sanctioned-export boundary so the local store keeps the raw provider input. The implementation reports record exact verified boundaries. Date: 2026-09-07.

## Boundaries

A local single-user system, without a network service, centralized database, container infrastructure, or custom agent harness. M1-M4 implement one Codex adapter serving CLI and local desktop coding; the surface is an observation attribute, not a separate core. Claude coding support follows in M-Anthropic / WD-022, after WD-014 and before WD-015. Ordinary chats are outside scope. Keep provider-neutral events and a small adapter interface without a general plugin framework. If the surface cannot be reliably identified, record `unknown`.

```text
Codex hooks -- adapter --+                         single user core
                        +-- redacted spool -- drain --+-- project A inbox -- SQLite + artifacts
Claude hooks - adapter -+   (resolve + build here)    +-- project B inbox -- SQLite + artifacts
transcripts (optional enrichment) --------------------+
CLI -- project selection -- read-only queries / queued mutations
```

## Processes and delivery

- `agent-watchdog hook codex` and `agent-watchdog hook claude` (the latter added in WD-022a) read JSON stdin and atomically write the complete provider input plus raw `cwd`, `event_id`, and `received_at` to a durable spool (WD-027/107). Until WD-115 lands, the adapter also redacts known credential forms across that input on the ingest path; WD-115 removes that step so the local store keeps raw input and the credential filter runs only at sanctioned export. `cwd` is transport-only; the adapter applies no semantic field projection. The daemon's spool drain resolves the registered project/worktree, applies size and quota limits, normalizes known fields, retains provider metadata, and admits the envelope to the project inbox; an unregistered or since-removed `cwd` is discarded there. New top-level provider fields are retained and produce a bounded WARNING without their values. Content capture defaults to true with global/project opt-out, applied at drain. Do not run Git diff, analysis, or LLM calls inside a hook. Use the provider's no-op response: empty stdout for Claude; an empty JSON object was exercised for Codex, including Stop/SubagentStop. Never return feedback or control fields in observation mode. Expected errors exit with code 0. WD-007 provides bounded persistent loss diagnostics.
- The adapter stamps the envelope ID and received time when it spools; the drain preserves both, so a replayed spool record yields the same envelope. Reprocessing is idempotent. Deduplicate repeated provider events only when a stable native ID is available, not merely by text hash.
- Optional daemon-wide pipeline telemetry stamps bounded stage-entry times and queue samples into envelopes after redaction. It can be disabled without changing the spool/inbox/SQLite delivery contract; it is global because native hooks do not resolve projects.
- The hook ensures a detached `agent-watchdog daemon run` starts if the core is unavailable. POSIX: a new session; Windows: a detached process without a window. Do not inherit the harness stdin/stdout/stderr handles.
- The Python core records bounded operational decisions in a global data-directory log. Its records use fixed codes, fixed provider names, a shape-bounded `error_type` (exception class name or internal failure code) and field name, and local Watchdog UUIDs only; prompts, paths, provider/session IDs, commands, tool output, payload values, exception messages, and tracebacks are excluded by default. Opt-in `[defaults] log_detail = true` appends one de-identified, 200-character `detail="..."` error string (credential forms removed, paths retained) for local debugging; it stays off for shipped defaults, exports, and shared reports. Logging is best effort and cannot change hook output or delivery.
- One OS-backed lock per user, held for the core's lifetime. Competing starts fail to acquire the lock and exit. PID/heartbeat are diagnostics, not the sole exclusivity mechanism. Never kill an unrelated process after PID reuse.
- The core lives until logout/stop; the next hook recovers a crash. This does not guarantee continuous operation without new events. Validate Codex CLI/desktop lifecycle on Windows in WD-005 and M1 acceptance; WD-022a lands the Claude observation adapter and installer; its live gate is met (see [verification](verification.md#wd-022a-claude-observation-gate-met)) across a CLI Pass A re-run and a supervised desktop pass; Claude enrichment, lifecycle beyond observation, and guidance/control stay in WD-022b; access to macOS/Linux hosts and compatibility verification are deferred to M5 / WD-019. Keep platform-specific launch/lock behavior isolated. Some process containers/job objects may restrict detachment; provide explicit user autostart as a fallback when that limitation is demonstrated.
- `daemon stop` sets a persistent pause and stops the core through the control inbox; new hooks neither start it nor accumulate events until `daemon start`. A crash does not set pause. `daemon status` distinguishes paused, running, unavailable, and degraded.
- The core commits events sequentially into each project's SQLite database; delete inbox entries only after commit. The spool drain is the same shape one stage earlier: delete a spool record only after its inbox write, and a crash in between replays safely because the envelope ID is stable. Put malformed records in bounded quarantine without blocking the entire queue.
- Poll inboxes every 250 ms while active, backing off to 2 s when idle. Store event time and received time separately; do not promise global ordering across processes. Late events recompute affected aggregates.
- Hooks do not wait for analysis or core readiness. Target full-call p95 latency of at most 250 ms on the measured machine, with a 2 s hook timeout. Measure Python startup and writing separately; do not hide their cost through async configuration.

## Projects and storage

Resolve user config/data/runtime directories with platformdirs. User TOML contains the registered project list and defaults. `project add <path>` explicitly registers a UUID, canonical root, and Git common-dir; worktrees resolve to that UUID. A separate clone gets a separate UUID. Identify non-Git projects by canonical root. Moving a project requires explicit `project relocate`, not merging by remote URL.

One directory per UUID: SQLite (WAL), inbox, artifacts, and quarantine. The registry contains no aggregate analytics. Store large raw outputs as separate artifacts; the database holds references, fingerprints, and bounded excerpts. Do not recursively scan user files: the transcript reader opens only an absolute Codex path previously recorded from a Codex hook and validates it against the observed session.

### Local fidelity and redaction

Watchdog observes one user's own coding sessions, whose prompts and outputs have already been sent to the provider, and it analyzes them locally without transmitting anything off the machine without the user's sanction. The per-project store is therefore not a sharing surface: it keeps the raw provider input, subject to `capture_content` and size limits. The credential filter is a bounded pattern matcher that can over-redact; it must not be the reason the only local copy of a session loses signal. WD-115 makes it an export-time transform: ingest and drain persist raw input, and the filter runs only when `inspection` writes an export intended to leave the machine. Collection scope (explicit registration, `daemon stop`, retention) still bounds what is kept.

The core is the only database writer. The CLI uses read-only connections; WD-011 labels, pins, and purges and WD-012 finding verdicts pass through a bounded control inbox with request IDs and acknowledgments. Lifecycle controls remain a single-slot desired-state file. Read-only reports and exports work while the core is stopped. Require a schema version and sequential migrations; reject unsupported newer schemas with a clear error and no overwrite.

Defaults are configurable in user TOML, with overrides by project UUID:

| Setting | Value |
|---|---:|
| Content / aggregates and labels | 30 / 180 days (`0` disables time expiry) |
| Total project quota, including WAL/inbox/artifacts | 2 GiB |
| Inbox within the total quota | 64 MiB |
| Maximum persisted event payload | 1 MiB |
| Process logs | 5 files of 10 MiB each |

Retention is a disk-budget control, not a privacy control: the data is the user's own, kept locally. Setting a day count to `0` disables time-based expiry for that class; the project quota and degraded-state loss counters still bound growth. WD-116 is pending: it implements the `0` disable path and the reframed defaults.

Delete expired content first, then old unpinned content, then old unpinned sessions. Pin protects against deletion, not quota accounting: if space cannot be reclaimed, stop accepting content/events and report degraded status with loss counters. Never silently delete pinned data. Reserve space for checkpoint/diagnostics; bound concurrent inbox writes with a short project lock. Account for auxiliary files; SQLite cleanup uses incremental vacuum/checkpoint support. Never delete existing vendor transcripts. See the implemented policy and operational limits in [storage](storage.md).

## Normalized events

Versioned envelope: `schema_version`, `event_id`, `provider`, `provider_version` (nullable), `surface`, `project_id`, `checkout_id`, `session_id`, `agent_id`, `parent_agent_id`, `turn_id`, `native_event_id`, `kind`, `occurred_at`, `received_at`, `source`, `payload`, and `availability`. Unavailable identifiers are nullable; do not invent them. Distinguish root sessions from subagents; leave unresolved ownership unassigned instead of mixing counters.

Kinds: session/turn lifecycle, tool start/finish, compaction, subagent lifecycle, waiting/interrupt, usage, and observation gap. Allow provider-specific payloads in a namespaced field. Preserve unsupported versions/events as unknown with a bounded payload. CLI JSON export uses the same versioned envelope; this is the first external data interface.

Hooks are the primary source. Transcript enrichment (`codex-rollout-v1` and, from WD-022b, `claude-transcript-v1` for the session and each subagent `agent_transcript_path`) runs only in the daemon from hook-provided absolute paths; it never persists transcript content. Durable per-source reader state handles partial lines, truncation, rotation, and resume. Bind readers to verified fixtures/versions. On a format change, disable only that enrichment source and persist an observation gap. Correlate usage by response, turn, and session IDs. Codex uses `thread_token_usage` as the sole cumulative scope and derives fieldwise deltas, never summing the three provider snapshots. Claude `message.usage` is per response, not cumulative, and is repeated on every content block; emit one `usage` event per distinct `requestId`, store each Anthropic counter raw, and never synthesise a `total_tokens` Anthropic does not report. Input/output/cached tokens are independently observed or unavailable. A Claude subagent `usage` row resolves to its parent conversation and carries the subagent's `agent_id`. Keep estimated and measured values separate. Subscription cost cannot be derived from token counts; defer monetary estimates.

## Analysis and reports

- Distinguish turn completion, session stop, and verified task outcome. Users label outcomes: success, partial, failed, abandoned, unknown. Task type is a free-form label with suggested bugfix/feature/refactor/docs/research values. A manual review adds a separate progress judgement (progress, slow, stuck, externally_blocked) and a per-finding verdict (true_positive, false_positive, uncertain); these are calibration inputs and no rule reads them.
- M2 rules: repeated command+outcome, identical error fingerprint, repeated failing-test set from comparable runs, compactions, and output size. Preserve rule versions, evidence IDs, counts, and explanations. An initial repetition threshold of 3 is a shadow-analysis hypothesis, not an intervention command. WD-010 implements these rules in the offline `report` view; findings remain observations, not control requests. Each finding also carries a `fingerprint` over its sorted evidence identifiers so a manual verdict survives recomputation; `diff_oscillation` evidence belongs to a checkout, not a session.
- Do not normalize commands aggressively: `pytest -x` changes the observed failure set. Compare test deltas only with matching target/configuration and completed results. The first structured parser covers pytest/JUnit; other outputs provide best-effort signals, not proof of acceptance.
- Compute debounced Git diff fingerprints per checkout in the core; concurrent agents/user edits make attribution uncertain. A->B->A is a signal, not proof of a stall. Snapshots must not modify the index or worktree.
- No universal no-progress score based on missing events. Distinguish observed, inferred, and unknown for every finding; waiting for a user/tool or incomplete data is not a stall. Exit code 0 does not automatically reset signals.
- CLI reports show timeline, coverage/gaps, active time separately from wall time, repetitions, available usage/compaction, and labels. Without evidence of interval start/end, active time is unknown.
- Export selected sessions as a Markdown summary, JSONL events, a manifest with versions/completeness/redaction, and a prompt template for manual LLM analysis. Export is the redaction boundary: the credential filter is applied here, over the raw stored input (WD-115). No automatic transmission. Clearly mark trace content as untrusted data; a content review before external sharing is recommended, not an enforced gate.

## Later extensions

Keep intervention separate from observations: proposal, evidence, required capability, expiry, and delivery receipt. M0-M2 do not implement placeholders for it. Begin with shadow calibration, then opt-in guidance at a native safe boundary. Stop continuation is not a hard pause. Do not assume App Server can attach to arbitrary desktop sessions: investigate session ownership/attach separately first.
