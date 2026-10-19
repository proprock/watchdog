# Architecture and contracts

Status: accepted design decisions. WD-003 [contracts](contracts.md), WD-004/007 [storage, retention, and redaction](storage.md), WD-005 [daemon](daemon.md), and WD-006 [hooks](hooks.md) are implemented. WD-008 provides the [observation CLI](cli.md); WD-009 adds Codex rollout-v1 usage enrichment; WD-010 adds deterministic, read-only shadow analysis and reports. WD-024 replaced the slow Python adapter with Rust, retaining the Python core. WD-149 now routes every event through one schema 2 spool directly into each project Store. WD-027 made that adapter spool-and-forget: it durably spools each event, and the daemon resolves the checkout, builds the envelope, and admits it. WD-109 packages that Rust binary as versioned GitHub Actions release artifacts for the supported host matrix. WD-110 makes it the only production hook entry point: `hooks install` (WD-006) always requires one, never silently installing the Python CLI's `hook` subcommand, which remains only as a developer/test utility with prepared-envelope publication through the same spool (see "Processes and delivery" below). WD-115 moves the credential filter off the ingest path to the sanctioned-export boundary so the local store keeps the raw provider input. The implementation reports record exact verified boundaries. Date: 2026-09-16. Later work (WD-139 to WD-168) is recorded in the amendments below; [Current versus superseded decisions](#current-versus-superseded-decisions-wd-169) says which text governs, and TODO.md holds open status.

## Boundaries

A local single-user system, without a network service, centralized database, container infrastructure, or custom agent harness. The Codex and Claude Code adapters each serve CLI and local desktop coding; the surface is an observation attribute, not a separate core. Control is enabled per provider, surface, exact version, event, tool and action, only where live evidence confirms the cell ([compatibility matrix](provider-compatibility.md)): Claude Code has the broader confirmed set, Codex CLI has the exact Windows profiles of WD-151, and Codex desktop is observation-only until WD-153. Ordinary chats are outside scope. Keep provider-neutral events and a small adapter interface without a general plugin framework. If the surface cannot be reliably identified, record `unknown`.

```text
Codex/Claude hooks -- native adapter --+
Developer/control envelopes ----------+-- schema 2 spool -- daemon -- project Store -- SQLite
Transcripts (optional enrichment) -------------------------+                       +-- artifacts
CLI -- project selection -- read-only queries / queued mutations
```

## Processes and delivery

- The native `agent-watchdog-hook` binary, invoked as `<codex|claude>` (the latter added in WD-022a) and the only production entry point since WD-110, reads JSON stdin and atomically writes the complete provider input plus raw `cwd`, `event_id`, and `received_at` to a durable spool (WD-027/107). The adapter removes only transport `cwd`; the local store keeps raw input and the credential filter runs only at sanctioned export (WD-115). The daemon's spool drain resolves the registered project/worktree, applies size and quota limits, normalizes known fields, retains provider metadata, and commits the envelope directly through the project Store; an unregistered or since-removed `cwd` is discarded there. New top-level provider fields are retained and produce a bounded WARNING without their values. Content capture defaults to true with global/project opt-out, applied at drain. Do not run Git diff, analysis, or LLM calls inside a hook. Use the provider's no-op response: empty stdout for Claude; an empty JSON object was exercised for Codex, including Stop/SubagentStop. A hook returns a control action only when an approved rule asks for it through the daemon's decision channel (WD-140; see "M4 LLM/control design" below), on a provider and event whose cell is confirmed live; every channel failure is fail-open and every other hook path stays silent. Each action is recorded with evidence, and a global and per-project kill switch disable them. `continue: false` and actions without a rule are forbidden. The first such rule is the same-model-subagent-spawn rule on Claude `PreToolUse` (`Agent`) from WD-022b, a deny then and a one-tier model rewrite since WD-142; the rule layer is described in [rules.md](rules.md). Expected errors exit with code 0. WD-007 provides bounded persistent loss diagnostics.
- The Python CLI's `agent-watchdog hook codex|claude` subcommand is a non-production developer/test utility (WD-110). It resolves the checkout and builds an envelope in `hooks.py::observe`, then calls `daemon.publish` to place a schema 2 `record_type: envelope` record in the shared spool. Native hooks publish `record_type: hook` records with raw input. `hooks install` always installs the native binary. Both delivery paths use the normal daemon-start and readiness mechanism.
- The adapter stamps the envelope ID and received time when it spools; the drain preserves both, so a replayed spool record yields the same envelope. Reprocessing is idempotent. Deduplicate repeated provider events only when a stable native ID is available, not merely by text hash.
- Optional daemon-wide pipeline telemetry stamps bounded stage-entry times and queue samples into envelopes. It can be disabled without changing the spool/SQLite delivery contract; it is global because native hooks do not resolve projects.
- The hook ensures a detached `agent-watchdog daemon run` starts if the core is unavailable. POSIX: a new session; Windows: a detached process without a window. Do not inherit the harness stdin/stdout/stderr handles.
- The Python core records bounded operational decisions in a global data-directory log. Its records use fixed codes, fixed provider names, a shape-bounded `error_type` (exception class name or internal failure code) and field name, and local Watchdog UUIDs only; prompts, paths, provider/session IDs, commands, tool output, payload values, exception messages, and tracebacks are excluded by default. Opt-in `[defaults] log_detail = true` appends one de-identified, 200-character `detail="..."` error string (credential forms removed, paths retained) for local debugging; it stays off for shipped defaults, exports, and shared reports. Logging is best effort and cannot change hook output or delivery.
- One OS-backed lock per user, held for the core's lifetime. Competing starts fail to acquire the lock and exit. PID/heartbeat are diagnostics, not the sole exclusivity mechanism. Never kill an unrelated process after PID reuse.
- The core lives until logout/stop; the next hook recovers a crash. This does not guarantee continuous operation without new events. Validate Codex CLI/desktop lifecycle on Windows in WD-005 and M1 acceptance; WD-022a lands the Claude observation adapter and installer; its live gate is met (see [verification](verification.md#wd-022a-claude-observation-gate-met)) across a CLI Pass A re-run and a supervised desktop pass; Claude enrichment, lifecycle beyond observation, and guidance/control stay in WD-022b; access to macOS/Linux hosts and compatibility verification are deferred to M5 / WD-019. Keep platform-specific launch/lock behavior isolated. Some process containers/job objects may restrict detachment; provide explicit user autostart as a fallback when that limitation is demonstrated.
- `daemon stop` sets a persistent pause through the desired-state file and stops the core; new hooks neither start it nor accumulate events until `daemon start`. A crash does not set pause. `daemon status` distinguishes paused, running, unavailable, and degraded.
- The core resolves a bounded spool window and commits batches directly to SQLite through one Store per project per tick. Queue files are deleted only after commit; a crash before acknowledgment safely replays the same event UUID and receipt identity. Raw and prepared envelopes receive the resolved project payload check. Malformed records enter bounded quarantine. There is no runtime event Inbox or legacy reader; schema 1 spool files and old pending Inboxes require an external one-shot migration preserving UUIDs, timestamps, provider metadata, and receipts.
- Poll the shared spool every 250 ms while active, backing off to 2 s when idle. Reuse each project Store for the tick; the measured default batch size is 100 (see [WD-149 evidence](evidence/wd149.md)). A single cooperative 200 ms maintenance budget is shared across writers, with round-robin scheduling and resumable cursors across Store opens. Pending cleanup or temporary capacity pressure retains work without a loss. Store event time and received time separately; do not promise global ordering across processes. Late events recompute affected aggregates.
- Hooks do not wait for analysis or core readiness. Target full-call p95 latency of at most 250 ms on the measured machine, with a 2 s hook timeout for observation hooks. Measure Python startup and writing separately; do not hide their cost through async configuration. A control subscription (a hook that waits for a daemon decision) is a separate installed entry with its own timeout of up to 10 s and its own budget, p95 of at most 500 ms; the 2 s limit is not raised to pass acceptance for observation hooks, and the installer change belongs to the task that adds the first subscription beyond the static one. *Status (WD-140, WD-142):* that separate entry was not built. A subscription shares the observation entry and its 2 s timeout; the adapter waits for the daemon at most `timeout_ms` (300 ms, clamped to 1500 ms) and fails open, so a slow daemon cannot cost more than that wait. WD-142 added the first subscriptions beyond the static one (`PostToolUse` on every tool and `PreToolUse` on `Bash`); a first measurement on a busy host gave a sequential p95 of 54-61 ms and up to 155 ms with four concurrent callers ([evidence](evidence/wd142-subscribed-bench.json)), inside the 500 ms budget. WD-154 owns the quiet-host re-measurement and the decision whether a separate entry with its own timeout is still needed.

## Projects and storage

Resolve user config/data/runtime directories with platformdirs. User TOML contains the registered project list and defaults. `project add <path>` explicitly registers a UUID, canonical root, and Git common-dir; worktrees resolve to that UUID. A separate clone gets a separate UUID. Identify non-Git projects by canonical root. Moving a project requires explicit `project relocate`, not merging by remote URL.

One directory per UUID holds SQLite (WAL), artifacts, and quarantine. Pending events share the data-directory spool, outside project directories. The registry contains no aggregate analytics. Store large raw outputs as separate artifacts; the database holds references, fingerprints, and bounded excerpts. Do not recursively scan user files: the transcript reader opens only an absolute Codex path previously recorded from a Codex hook and validates it against the observed session.

### Cross-project views (WD-016)

Decision: a cross-project view is a read-only fan-out over the per-project stores, never a merged database or a registry-level aggregate. `summary`, `overview`, and `insights --all-projects` (WD-132) each open one short read-only snapshot per project and combine the results in memory; the result is a set of known observations, not a global transaction, and an unreadable or absent project stays unknown instead of zero. `overview` is the shared per-project picture: counts, token use with an optional list-price estimate, finding counts per rule, and last activity. Since WD-141 its findings come from the per-session findings the daemon materializes (`session_findings`, schema v8), not from re-analyzing every event: they are "as of" each session's `computed_at`, the result reports `as_of` and `pending_sessions`, and every session is analyzed alone. `report` still analyzes the events, so it stays reproducible.

A local web UI is not built with WD-016. It is recorded as the next milestone (WD-136, split into a, b, and c) with an entry gate: build it only if the `overview` JSON proves insufficient in daily use. `overview` took 9.3 s, mostly per-call re-analysis; WD-138 removed redundant parsing and `json_extract` scans and brought it to about 5.1 s in process (6.4 s through the CLI). WD-141 removed the analysis from the call: the daemon keeps the findings warm in `session_findings`, and `overview` on a copy of the live stores took 1.4-1.9 s, of which the findings are 0.3 s ([evidence](evidence/wd141.md)). The UI would be convenience only.

Decision: the UI is a separate, on-demand process (`agent-watchdog ui`), not part of the core daemon. The daemon is shared by Codex and Claude observation and must not fail because of an optional surface (the same rule as the WD-022b policy socket), and a long analysis or a configuration write must not compete with polling. The UI reuses `inspection.overview`, stays loopback-only, and adds no plugin or framework layer. It grows in three steps with different risk:

- **WD-136a, viewer.** Read-only: `overview` and saved `insights` reports. It opens the stores read-only and writes nothing.
- **WD-136b, generation.** A report is generated only when the user presses a button, after the expected cost and duration are shown, as a background job through the same isolated `claude -p` path as the CLI. This keeps the rule that LLM calls need an explicit user opt-in.
- **WD-136c, apply.** The only step that writes. Recommendations carry `scope`, `target_file`, and `project`, so writes go to a closed allowlist: project or user `CLAUDE.md`, `AGENTS.md`, `settings.json` permissions, and Watchdog `log`-only rule candidates. The user selects items (unselected by default) and presses one Apply; before any write the UI shows a diff and asks for confirmation, then backs up, writes atomically, and records what was applied. Model text is data and is never executed. A write-capable local server adds a per-instance token and `Host`/`Origin` checks against CSRF and DNS rebinding, following the policy-socket discovery pattern. Tests never write to real provider configuration. The detailed design (targets per provider, merge semantics, rollback, dedupe of applied items) is decided here before any code.

### Local fidelity and redaction

Watchdog observes one user's own coding sessions, whose prompts and outputs have already been sent to the provider, and it analyzes them locally without transmitting anything off the machine without the user's sanction. The per-project store is therefore not a sharing surface: it keeps the raw provider input, subject to `capture_content` and size limits. The credential filter is a bounded pattern matcher that can over-redact; it must not be the reason the only local copy of a session loses signal. WD-115 makes it an export-time transform: ingest and drain persist raw input, and the filter runs only when `inspection` writes an export intended to leave the machine. Collection scope (explicit registration, `daemon stop`, retention) still bounds what is kept. The `insights` bundle redaction follows the same logic: the bundle goes to the user's own provider account, so the decision (implemented with the judge task, WD-145) is an `insights_redact` option, default `true` for compatibility, that lets a local user keep the lines the filter would remove.

The core is the only database writer. The CLI uses read-only connections; WD-011 labels, pins, and purges and WD-012 finding verdicts use bounded command request files with request IDs and acknowledgments. Lifecycle controls remain a single-slot desired-state file. These are command mechanisms, not additional event Inboxes, and WD-149 leaves them intact. Read-only reports and exports work while the core is stopped. Require a schema version and sequential migrations; reject unsupported newer schemas with a clear error and no overwrite.

Project defaults are configurable in user TOML, with overrides by project UUID. The spool cap is daemon-wide. `payload_bytes <= project_bytes`; old `inbox_bytes` entries are accepted but ignored with a warning:

| Setting | Value |
|---|---:|
| Content / aggregates and labels | 30 / 180 days (`0` disables time expiry) |
| Total project quota, including WAL/artifacts | 2 GiB |
| Shared event spool, outside project quota | 64 MiB / 4096 files |
| Maximum persisted event payload | 1 MiB |
| Process logs | 5 files of 10 MiB each |

Retention is a disk-budget control, not a privacy control: the data is the user's own, kept locally. Setting a day count to `0` disables time-based expiry for that class; the project quota and degraded-state loss counters still bound growth.

Delete expired content first, then old unpinned content, then old unpinned sessions. Pin protects against deletion, not quota accounting: if space cannot be reclaimed, stop accepting content/events and report degraded status with loss counters. Never silently delete pinned data. Reserve space for checkpoint/diagnostics; serialize queue publishers with the shared data-directory admission lock and Store writes with a short project lock. Account for auxiliary files; SQLite cleanup uses incremental vacuum/checkpoint support. Never delete existing vendor transcripts. See the implemented policy and operational limits in [storage](storage.md).

## Normalized events

Versioned envelope: `schema_version`, `event_id`, `provider`, `provider_version` (nullable), `surface`, `project_id`, `checkout_id`, `session_id`, `agent_id`, `parent_agent_id`, `turn_id`, `native_event_id`, `kind`, `occurred_at`, `received_at`, `source`, `payload`, and `availability`. Unavailable identifiers are nullable; do not invent them. Distinguish root sessions from subagents; leave unresolved ownership unassigned instead of mixing counters.

Kinds: session/turn lifecycle, tool start/finish, compaction, subagent lifecycle, waiting/interrupt, usage, and observation gap. Allow provider-specific payloads in a namespaced field. Preserve unsupported versions/events as unknown with a bounded payload. CLI JSON export uses the same versioned envelope; this is the first external data interface.

Hooks are the primary source. Transcript enrichment (`codex-rollout-v1` and, from WD-022b, `claude-transcript-v1` for the session and each subagent `agent_transcript_path`) runs only in the daemon from hook-provided absolute paths; by default it persists no transcript content. Decision (implemented by the judge task, WD-145): an optional capture of the assistant's text, governed by `capture_content`, may feed the semantic judge, because it is the best evidence of what the agent intended. Durable per-source reader state handles partial lines, truncation, rotation, and resume. A transcript line is read whole, across read windows, up to 32 MiB (WD-130: real Codex `item_completed` lines reach 6.7 MiB, and the former 1 MiB limit stopped such sources for good). A longer line is skipped with a `transcript_line_oversized` gap. A Claude response too large to hold open is emitted with its last-seen usage and a `claude_response_oversized` gap, because that value may undercount. No line is dropped silently. Bind readers to verified fixtures/versions. On a format change, disable only that enrichment source and persist an observation gap. Correlate usage by response, turn, and session IDs. A Codex usage event is identified by its session and `response_id`, never by the transcript path spelling or line offset, and the reader skips a response already stored for that session under any event ID (WD-128: Codex reports the same rollout as both `C:\...` and `\\?\C:\...`). A Claude usage event is likewise identified by its conversation, agent, and `requestId`, and the Claude reader skips a request already stored for them (WD-129). A Codex hook fired inside a subagent reports the subagent's own rollout (`rollout-<time>-<thread id>.jsonl`) under the parent session. The reader reads it as that subagent (usage `agent_id` = thread id) once `session_meta` names the thread and the parent session; otherwise it records a `subagent_meta_mismatch` gap (WD-131). Such usage inherits a model or effort only from the same agent's hooks. A source whose file does not exist and was never read is unobserved, not a failure (WD-137): it records no gap, never marks a project degraded, and stays registered so the file is read if it appears. Claude Code creates the session file at the first prompt, after the `SessionStart` hook that names it, and after every turn it fires a `SubagentStop` with an empty `agent_type` for a helper agent whose `agent_transcript_path` it never writes (191 of 191 such events on the live store had no file). Only a file that was read and then vanished, or another I/O error, is `transcript_unreadable`. Rows the earlier path-keyed reader stored twice stay in place, because stored events are authoritative. Usage reads (`usage`, `--price`, `report`, `export`, `insights`) count each stored response (its `native_event_id` within a conversation) once, taking its first stored copy. Codex uses `thread_token_usage` as the sole cumulative scope and derives fieldwise deltas, never summing the three provider snapshots. Claude `message.usage` is per response, not cumulative, and is repeated on every content block; emit one `usage` event per distinct `requestId`, store each Anthropic counter raw, and never synthesise a `total_tokens` Anthropic does not report. The reported value can grow across a response's own blocks (confirmed on real subagent transcripts: `output_tokens` commonly grows from single digits to its true final value); a response is held open until a block reports a non-null `stop_reason`, or is treated as closed by a later, distinct `requestId` superseding it, so the emitted counters are the final value, not the first. Content-free scans of real transcripts found this growth only on subagent (sidechain) responses — main-conversation responses always repeat the same, already-final value across their blocks — and found a small minority of subagent responses that never report a `stop_reason` at all; those flush with their last-known value once the source stops growing (a single read window with no new bytes), an accepted residual limitation shared with the existing partial-line tail cap (both bounded at well under the `transcript_sources.tail` column's 1 MiB limit). An event id already stored under the prior first-block-wins behavior is never re-emitted with a different value under the same id; it keeps its original value, another accepted, explicit limitation rather than a receipt-identity conflict. Input/output/cached tokens are independently observed or unavailable. A Claude subagent `usage` row resolves to its parent conversation and carries the subagent's `agent_id`. Keep estimated and measured values separate. Subscription cost cannot be derived from token counts; defer monetary estimates.

## Analysis and reports

- Distinguish turn completion, session stop, and verified task outcome. Users label outcomes: success, partial, failed, abandoned, unknown. Task type is a free-form label with suggested bugfix/feature/refactor/docs/research values. A manual review adds a separate progress judgement (progress, slow, stuck, externally_blocked) and a per-finding verdict (true_positive, false_positive, uncertain); these are calibration inputs and no rule reads them.
- M2 rules: repeated command+outcome, identical error fingerprint, repeated failing-test set from comparable runs, compactions, and output size. Preserve rule versions, evidence IDs, counts, and explanations. An initial repetition threshold of 3 is a shadow-analysis hypothesis, not an intervention command. WD-010 implements these rules in the offline `report` view; findings remain observations, not control requests. Each finding also carries a `fingerprint` over its sorted evidence identifiers so a manual verdict survives recomputation; `diff_oscillation` evidence still belongs to a checkout, not a session, and its manual verdict stays keyed by checkout (WD-013).
- `wd-010.v2` (WD-146) corrects what v1 could not see. One `analysis.tool_outcome(payload, provider)` serves the report, `inspection.finish_status`, the daemon's session state and the calibration tool: Claude's `PostToolUseFailure` is a failure (`is_interrupt` makes it an `interrupt`, not an agent error), a structured `exit_code`/`isError` comes next, and Claude's `PostToolUse` is a success; absent result evidence is `unknown`. WD-164 extends the available Codex evidence through the shared execution projection below. `repeated_tool_outcome` groups per agent and counts a chain of at least three repeats only when each followed no finished edit tool or reproduced the previous output. `identical_error` groups on the exit code and the error text with paths, quoted values, hex ids and numbers normalised, reading Claude's failure-hook `error` because a failed call has no `tool_response`. `diff_oscillation` needs an edit or a turn start between each pair of snapshots, and filters nothing when events or snapshot times are missing. v1 verdicts do not apply to v2 findings (the fingerprint includes the rule version); `scripts/calibrate.py report --rule-version wd-010.v2 [--review FILE]` recomputes a frozen cohort and compares by shared evidence ([measurement](evidence/wd146-recalibration.md)).

- WD-164 adds `execution.normalize_events` (`wd-164.v1`) for Codex hook and rollout observations. Native session/agent/call identities join `Bash` hooks with `exec_command` records; execution session IDs join yielded commands with their `write_stdin` completion. Polls remain waiting observations. Commands (`command`/`cmd`), native nested `CommandExecution`/`FileChange` items, cancellation and structured/frame exit evidence feed the same state, report and insights projection. Raw events stay unchanged; missing content, ambiguous identities, conflicting results and code-mode wrapper completion never prove command success. Rollout tool grammars are version-bound independently of usage enrichment. Late evidence keeps the original completion position; Codex state caches rebuild once per projection version from retained events. Statistical thresholds and rule bodies remain unchanged, while Codex finding fingerprints include the execution projection so earlier verdicts are not reused silently. See [WD-164 evidence](evidence/wd164.md); polling calibration and broader cache invalidation remain WD-161/WD-167 and WD-162 work.
- Do not normalize commands aggressively: `pytest -x` changes the observed failure set. Compare test deltas only with matching target/configuration and completed results. The first structured parser covers pytest/JUnit; other outputs provide best-effort signals, not proof of acceptance.
- Compute debounced Git diff fingerprints per checkout in the core. Since WD-121 (2026-09-30) a fingerprint covers unstaged and staged tracked changes plus untracked, non-ignored files, bounded and unknown on any failure ([coverage](storage.md#checkout-fingerprint-coverage-wd-121)); earlier snapshots covered unstaged tracked changes only, under the same `wd-010.v1` rule version. Concurrent agents/user edits make attribution uncertain. A->B->A is a signal, not proof of a stall. Snapshots must not modify the index or worktree. WD-118 narrows which sessions a checkout-wide oscillation is *reported to*: `inspection.snapshots_for_checkouts` tags each snapshot with the sessions whose observed turn window (`turn.start`/`turn.end`) covered its capture instant, and `analyze` only surfaces the finding to a session in that set. An oscillation whose three snapshots span more than one session keeps its existing "uncertain" attribution — a concurrent editor is not excluded. An oscillation with no session at all attributable (no turn boundary observed for the checkout) still surfaces to every session touching the checkout, matching the prior behavior, because narrowing on no evidence would manufacture false precision rather than reduce it.
- No universal no-progress score based on missing events; a deterministic rule on the presence of events remains allowed. Distinguish observed, inferred, and unknown for every finding; waiting for a user/tool or incomplete data is not a stall. Exit code 0 does not automatically reset signals.
- CLI reports show timeline, coverage/gaps, active time separately from wall time, repetitions, available usage/compaction, and labels. Without evidence of interval start/end, active time is unknown.
- Export selected sessions as a Markdown summary, JSONL events, a manifest with versions/completeness/redaction, and a prompt template for manual LLM analysis. Export is the redaction boundary: the credential filter is applied here, over the raw stored input (WD-115). No automatic transmission. Clearly mark trace content as untrusted data; a content review before external sharing is recommended, not an enforced gate.

## Later extensions

Keep intervention separate from observations: proposal, evidence, required capability, expiry, and delivery receipt. M0-M2 do not implement placeholders for it. Begin with shadow calibration, then opt-in guidance at a native safe boundary. Stop continuation is not a hard pause. Do not assume App Server can attach to arbitrary desktop sessions: investigate session ownership/attach separately first.

## M3 guidance policy (WD-013)

The [2026-10-18 WD-147 review](#wd-147-calibration-and-promotion-review-2026-10-18)
supersedes mandatory threshold enforcement below as the planned default:
eligibility is advisory, with explicit human confirmation for below-threshold
promotion; strict enforcement remains configurable. It also fixes the independent
sample unit, deterministic criteria, changed-body grants and human-confirmed
prefill below. Historical calibration results and current delivery status remain
unchanged; specification review is resolved and implementation remains open.

This section is the M3 decision gate: it fixes the delivery mechanism and applies the WD-012 calibration to select rules. It is a design decision, not an implementation; M3 is only built once a rule actually clears the gate below.

**Mechanism, for whenever a rule qualifies:**

- Observe remains the default for every project; guidance requires explicit per-project opt-in, separate from the existing `content_capture` opt-out. A global kill switch stops delivery immediately regardless of per-project opt-in, with no residual nudge in flight.
- A delivery record is distinct from an observation: it carries the source finding's evidence IDs, the capability it required, an expiry, and a cooldown. At most one nudge per blocker fires until new evidence appears for it; a blocker that keeps re-observing the same evidence does not re-notify.
- Record `proposed`, `delivered`, and `response_observed` as three separate facts. Delivery is not acceptance: a nudge the user ignores, argues with, or contradicts is not itself evidence the finding was wrong, and a nudge followed by a change is not proof the finding was right. Advice-acceptance measurement is a separate, later analysis, never folded into precision.
- Provider-specific safe delivery only: a nudge is injected solely at a boundary a provider adapter can support without altering control flow (see the CLI/hook contracts); an unsupported provider/surface combination stays observe-only.

**Rule selection, applying the gate to [wd012-calibration.md](evidence/wd012-calibration.md):**

The release gate depends on the action a rule may take (ROADMAP M3): `log` needs none; `context` needs at least 60% session-scoped precision over 10 reviewed cases; `ask` 80% over 20; `deny` or `block` 90% over 30, each on a declared, disclosed-sample-size dataset. A deterministic rule skips the gate for `log` and `context` and reaches `ask` or `deny` after 10 observations without a false positive. The single former 90% bar priced a hint and a denial equally and effectively closed M3; the ladder replaces it. The table below applies the strictest rung to the 24-session frozen cohort. Generated from the 24-session frozen cohort (`scripts/calibrate.py report`, `wd-012.calibration.v3`):

| rule | observed | precision | gate result |
| --- | --- | --- | --- |
| `repeated_tool_outcome` | 8 | 25.00% (2 TP / 6 FP) | fails; well below 90% at a reviewed sample of 8, not a borderline small-sample case |
| `identical_error` | 0 | null | fails; zero observations in this cohort is no evidence, not a pass |
| `repeated_test_failure` | 0 | null | fails; zero observations in this cohort is no evidence, not a pass |
| `diff_oscillation` | 8 | unreviewed (checkout-scoped) | not eligible for session-scoped delivery; still checkout-scoped by decision, though WD-118 has landed the attribution mechanism a future gate re-run would score against |

**Decision:** no `wd-010.v1` rule is selected for M3 delivery. The mechanism above is specified so a future qualifying rule has a fixed target to implement against, but no code path delivers guidance today; all four rules stay observe-only, matching the M0-M2 boundary already in place. The 4 recorded false negatives (slow sessions with no session-scoped finding) mean even a precision fix would leave recall gaps; do not read a future precision pass as sufficient on its own.

**`wd-010.v2` re-measure (WD-146, [evidence](evidence/wd146-recalibration.md)):** on the 46-session WD-122 cohort `repeated_tool_outcome` fires 4 times instead of 19 (12 of 15 reviewed false positives dropped, one of two true positives lost) and `identical_error` fires twice instead of never. None of the six v2 findings has a recorded verdict yet, so v2 precision is unmeasured and the table above stays the only gate evidence; the decision is unchanged.

**Re-opening this gate:** a rule is promoted only by a fresh `scripts/calibrate.py report` run showing it at or above 90% precision with its sample size, not by arguing around the numbers above. WD-118 changed `diff_oscillation` attribution (`inspection.snapshots_for_checkouts` narrows which sessions a checkout-wide oscillation is reported to, using observed turn windows) and demonstrated the fix with offline tests (`tests/test_analysis.py`, `tests/test_observation_cli.py`); it did not itself flip `CHECKOUT_SCOPED_RULES` or re-run calibration against the live store, so the numbers in this table are still the pre-WD-118 baseline. Re-run `scripts/calibrate.py sample`/`report` against the live store, and decide separately whether the now-attributable rule should move out of `CHECKOUT_SCOPED_RULES`, once any rule-logic fix lands or the live cohort has grown past the current 24 sessions.

## M4 LLM/control design (WD-014)

The [2026-10-18 WD-145 review](#wd-145-semantic-judge-review-2026-10-18)
supersedes the historical calibrated-trigger requirement for diagnostic analysis
and the proposed default context delivery below. It also resolves cancellation,
budget accounting, result scope and default limits; this is a planned design.

This section is the M4 execution/control decision gate: it fixes how a semantic-judge LLM call would be invoked, bounded, isolated, and escalated, if and when a future trigger is authorized, and separately fixes a rule system for deterministic policy rules that need no calibration. It is a design decision, not an implementation; nothing below installs a hook, calls a model, or changes runtime behavior.

**Execution mechanism:** local analysis calls the provider's own CLI in headless mode (`claude -p ...` / `codex exec ...`), not a raw provider API client — this reuses the user's already-authenticated CLI/subscription, avoids adding an API-key/secrets surface, and reuses the existing no-window subprocess wrapper (`src/agent_watchdog/_proc.py::run`/`popen`). A hosted API path is out of scope until a CLI limitation is demonstrated. The call happens only from the daemon or an explicit user-run CLI command, never inside a hook (existing invariant above: "Do not run Git diff, analysis, or LLM calls inside a hook"). Every call records provider name, CLI binary path, and version (`--version`) as provenance, alongside the evidence bundle sent and the structured response received.

**Opt-in and kill switch:** disabled by default everywhere. WD-148 confirmed that the daemon may make automatic calls on a project that opts in, within its budget and timeout, with provenance and a kill switch (TENETS.md); the implementation is WD-145. A project needs an explicit opt-in flag, `semantic_judge_enabled` (default `false`), distinct from `content_capture` and the WD-013 `guidance` opt-in. A global `semantic_judge_enabled = false` at the top-level `[defaults]` scope stops all future calls immediately regardless of a project override (mirroring how `capture_content` composes today). The historical finish-in-flight-only policy is superseded by the [2026-10-18 cancellation decision](#wd-145-semantic-judge-review-2026-10-18): cancel owned provider calls by default, with finish-within-timeout available in configuration and no delivery while paused or disabled.

**Budget and timeout — explicit, named config settings, not hidden constants:** `semantic_judge_timeout_seconds` (default `120`, selected in the 2026-10-18 WD-145 review, superseding the former `60`) is the per-call wall-clock timeout; exceeding it ends the owned call and records `unavailable` with a timeout reason, with no automatic retry. `semantic_judge_daily_call_budget` (default `5`) is the per-project daily call cap; exhausting it refuses new calls with an inspectable `unavailable` reason rather than queuing or silently skipping. Settings are planned under `[defaults]` with project overrides, but examples and readers must ship together. The [current WD-145 decision](#wd-145-semantic-judge-review-2026-10-18) defines durable reservation, failed-attempt accounting and recovery. Budget exhaustion, timeout, nonzero exit or malformed output never become `progress`/`stuck` verdicts and never block or delay hook responses.

**Exclusion of the product's own analytical sessions (recursion control):** the spawned CLI call runs against an isolated, throwaway config/home directory with Watchdog hooks NOT installed into it, not merely tagged or filtered after the fact, so the call has no hook wiring through which to recurse into the event spool. If isolation cannot be established (for example, the CLI needs the user's real config to authenticate), the call is refused rather than run un-isolated. The bundle sent to the model never includes Watchdog's own source, logs, or database.

**Facts/evidence bundle and structured output:** the input is a compact bundle built from already-stored evidence (findings, evidence IDs, fingerprints, bounded excerpts), never a raw transcript re-send; it reuses the existing WD-011 export-bundle shape (`inspection.export_sessions`) as its base format. The output is a structured `progress|uncertain|stuck|blocked` judgment plus free-text rationale; a summary is never treated as objective verification, and it never overwrites an M2 deterministic finding.

**Escalation ladder and human gate:** the ladder is advisory -> replan request -> read-only second opinion -> question for the user, and a caller need not walk every step for every signal. A "question for the user" delivers at the same safe hook boundary as the WD-013 guidance mechanism (a textual nudge) — explicitly not a forced pause/resume; Watchdog never auto-grants an approval. An actual pause/resume/attach capability requires a confirmed adapter contract. WD-015 found that Codex App Server has no documented generic attach contract for arbitrary running CLI or desktop sessions, so no such Codex adapter exists today and this stage remains theoretical.

**Amendment (WD-123, manual insights):** the first implemented model call is the user-run `agent-watchdog insights` command ([cli](cli.md#insights)), and it revises three points above for that manual path only.
- **Isolation.** It comes from `claude -p` flags rather than a throwaway config home. The user's subscription login lives in that home, so a separate one would break authentication. `--safe-mode` disables hooks, CLAUDE.md, skills, plugins and MCP servers while keeping auth. `--setting-sources ""`, `--strict-mcp-config`, `--tools ""` and `--no-session-persistence` remove the rest. The call runs from a scratch directory outside every registered root.
- **Refusal.** The WD-014 refusal clause still applies: without `--safe-mode` the call is refused. A live probe confirmed that no analysis session reached any project store (see [verification](verification.md#wd-123-insights-live-probe)).
- **Opt-in and limits.** For this manual command the explicit invocation is the opt-in, replacing a per-project `semantic_judge_enabled`. Its kill switch is `insights_llm_enabled` in `[defaults]` or project overrides. `--timeout` (default 600 s, well above a measured 148 s call because a window-scaled bundle adds input time) replaces `semantic_judge_timeout_seconds` for the manual path. The bundle budget scales with the context window the model reported on its previous call, remembered per model in the data directory. No daily budget applies to a call the user starts by hand.
- **Reserved names.** `semantic_judge_enabled`, `semantic_judge_timeout_seconds` and `semantic_judge_daily_call_budget` remain reserved for any future automatic trigger.
- **Backend selection (WD-166, partial).** Manual insights and calibration suggestions
  select an analysis backend, model and effort independently of the observed session
  provider. Both reuse `Request`/`Result`/`Runner`; Claude remains the default and no
  failure changes providers. Context-size memories are namespaced by backend/model;
  they are distinct from future daemon call budgets. The Codex backend currently
  refuses analysis with `isolation_unavailable` after offline preflight because no
  complete isolation contract has been established. It does not launch a model or
  read/copy authentication. [WD-166 evidence](evidence/wd166.md) lists the remaining
  execution and live-verification gates.
- **Bundle.** It is a redacted evidence bundle, an export boundary under WD-115, ranked and fitted to a token budget. It is not the raw `export_sessions` event dump: real sessions exceed any context window (p90 per-session content was about 1 MB on the live store).
- **Session triage (WD-133).** `insights sessions` reads every session of a project window into compact, trouble-ranked facets and makes one call that names candidate stuck, blocked, or abandoned sessions and recurring session-level patterns. It makes no per-session calls, never labels a session, and stores nothing; the user opens a candidate with `insights session`.
- **Cross-mode digest (WD-134).** `insights digest` builds six modes' evidence for one window, fits each into a fixed share of the budget, and makes one call that links findings across modes and ranks a short action list. A link counts as cross-mode only when its cited item IDs span two modes; it changes nothing stored. WD-149 reads one short, read-only project snapshot and shares parsed and normalized window data across the modes, avoiding repeated event parsing or independent snapshot scans.
- **The judge itself (WD-126).** `insights session` is this section's manually invoked second opinion. It returns the structured `progress|uncertain|stuck|blocked` judgement with a rationale, grounded evidence IDs, and a next step, and it never overwrites or stores anything. `scripts/calibrate.py annotate` offers it only on request (`s`), labelled "LLM suggestion, not a verdict". No automatic trigger exists; the re-opening conditions below still govern one.

**Deterministic policy rules (no calibration required):** distinct from the M2/M3 statistical rule set, a policy rule encodes a structural fact that is true or false by construction — for example, a subagent's resolved model ID equals its coordinator's, using the per-turn model resolution WD-022b delivers — not a probabilistic judgment about progress or stalls. The WD-013 90% precision gate does not apply to this class; correctness is definitional, so a policy rule may be enabled without a calibration cycle. Every policy rule declares one action: `log` records a full-fidelity finding through the normal event/finding path (subject to `capture_content` and retention/quota, redacted only at export per WD-115) — the triggering fact, its evidence IDs, and the exact tool input/prompt passed — through the normal finding path rather than the bounded operational diagnostic log, which deliberately excludes prompts and content; `intervene` additionally denies or alters the current call at the hook boundary itself, only where the provider adapter has a confirmed blocking capability; `both` logs unconditionally, then attempts `intervene`. The first rule, shipped as the concrete example informing this design and not an M2 shadow rule, is same-model subagent spawn (a subagent's resolved model equals its coordinator's), with action `both`: `log` always records the launch fact and the subagent's exact prompt/tool input as evidence; `intervene`, confirmed capability today on Claude only, at `PreToolUse` (already mapped to `tool.start`, see "Normalized events" and `docs/hooks.md`), denies the tool call and returns an error telling the caller to downgrade the subagent's model tier — the first rule to use the control path that the TENETS control invariant now governs generally. On Codex, or any provider without a confirmed blocking capability, `intervene` degrades to `log` automatically (the tool call proceeds unmodified) plus a bounded warning recorded in the same finding, so the capability gap stays visible for later review rather than silently dropped. Rules are declared as `{name, condition, action}`; adding a new deterministic policy rule is a config/implementation change, never itself requiring a fresh WD-013-style precision gate, which applies only to a rule whose condition is a probabilistic judgment.

This design originally required one narrow exception to an observation-only hook invariant. WD-148 replaced that invariant with the general control invariant in TENETS.md: a hook returns an action only for an approved rule, through the decision channel, on a confirmed provider capability, fail-open, recorded with evidence, and behind a kill switch. A provider without confirmed capability still degrades `intervene` to `log` plus a recorded warning rather than silently doing nothing.

**Decision:** the policy above (CLI-mode calls, explicit opt-in, kill switch, the named `semantic_judge_timeout_seconds`/`semantic_judge_daily_call_budget` config settings, isolated non-observing analysis sessions, bundle/output shape, ladder/gate boundaries, the deterministic policy-rule system, and the narrow invariant exception) is fixed as the target for whenever M4 is implemented. It does not itself authorize any automatic semantic-judge call: applying it to the M2/M3 results already recorded ([wd012-calibration.md](evidence/wd012-calibration.md) and the WD-013 gate table above), no M2 rule cleared the 90% precision bar, so there is no calibrated trigger to feed an escalation ladder today. M4 therefore ships, if at all, only as a manually user-invoked "second opinion" (bundle -> CLI call -> structured judgment in a report), never an automatic trigger from an M2 rule, until a rule is separately promoted. The deterministic policy-rule system and its first rule are fixed by this decision but not implemented by it; implementation is separate future work, most likely folded into WD-022b, since detecting "subagent model equals coordinator model" needs the per-turn model resolution WD-022b already delivers. Superseded in part by WD-148: the daemon's automatic judge is now permitted under the project opt-in, daily budget, timeout, and kill switch named above (implementation WD-145), and the calibrated-trigger requirement is replaced by the per-action precision ladder in the M3 section.

**Re-opening this gate:** an automatic statistical (M2-rule-triggered) escalation path opens only when a future WD-013-style gate re-run promotes an M2 rule at or above 90% precision with disclosed sample size, or a separate future decision defines semantic-judge-specific triggering criteria independent of the current rule set. This re-opening clause does not gate deterministic policy rules, which may be added or enabled at any time as a config/implementation change since their condition is not a statistical judgment. A real pause/resume human gate for the semantic-judge ladder opens only after a future adapter demonstrates a concrete, adapter-verified ownership/attach/steer/interrupt contract; WD-015 found no generic Codex App Server contract for arbitrary existing CLI or desktop sessions. This is separate from the already-decided `intervene` exception for policy rules, which does not wait on WD-015. The budget/timeout defaults above may be revised once real latency/cost data exists from the manual-invocation path.

**Implementation (WD-022b, the `log` half):** `agent_watchdog.analysis.same_model_subagent_spawn` implements the first deterministic policy rule's unconditional `log` step, under its own `POLICY_RULE_VERSION` ("wd-014.v1") separate from the M2/M3 statistical `RULE_VERSION`. Each finding carries `"action": "both"`, the rule's own WD-014-declared action (not merely "log": this function only performs the log step itself, but the finding names the rule's actual configured action; the `intervene` attempt happens separately and synchronously, below, and is not itself recorded onto the finding), one finding per subagent spawn (`agent_id`), not per usage event (a subagent's own response can produce several usage rows). The model comparison walks a session's already-persisted envelopes directly (each Claude usage event already carries its own observed `payload.claude.model`, from `transcripts.py`'s `claude-transcript-v1` reader), ordered by `occurred_at` (the true API response time) rather than `received_at` (ingestion time): a subagent transcript is registered only at `agent.end` and enriched afterwards, so its usage rows can be received long after a later coordinator model switch even though they occurred before it, and only `occurred_at` order compares against the model genuinely active at the time. The most recently occurring coordinator usage event's model (`agent_id is None`) is compared against each subagent usage event's own model (`agent_id` set); a subagent whose model has not yet been observed is never compared, not treated as a false match. Evidence is the matching `agent.start` event (the launch fact) plus the first matching subagent usage event; the spawning tool call's own prompt/tool input, on its `PreToolUse`/`tool.start` event, carries no `agent_id` yet at that point and cannot be linked here — the "exact tool input/prompt" evidence the policy-rule design describes is not available at this layer for this rule; a future `intervene` implementation instead compares synchronously at `PreToolUse` time, where the tool input is directly at hand. This reuses the model each response already reports, rather than schema-v6's `event_facts`/`_inherited_config` inheritance table, which resolves a *missing* model by carrying one forward — not needed here since a `usage` event's own model is always directly observed by construction. `intervene` (the Claude-`PreToolUse`-only deny path) is implemented separately (below). The `both` action's degrade-to-`log` behavior on non-Claude adapters is not implemented, and cannot be reached today: `same_model_subagent_spawn` itself only ever evaluates `provider == "claude"` events (written when Codex usage events carried no per-subagent model attribution; WD-165 later added observed Codex attribution, but this rule was not extended to use it) -- there is no Codex finding for a degrade warning to attach to, not a degrade that silently produces one anyway. Codex's adapter correspondingly has no policy-socket branch at all and always takes the unmodified fire-and-forget path, which is a `provider`-level exclusion, not an instance of the WD-014 degrade mechanism.

**Implementation (WD-022b, `intervene`):** the native adapter never gained a general blocking round-trip; WD-027's "spool-and-forget" design (fire-and-forget, no listener anywhere) stands for every other event and provider. Exactly one narrow addition exists: while the daemon is running, it also runs a loopback-only (`127.0.0.1`, binding wider would trigger a Windows Firewall prompt for no benefit) TCP listener on a background thread, publishing `{schema_version, pid, port, token}` to `<data>/policy/socket.json` -- the same discovery-file pattern `spool/limits.json` already uses, with a random per-instance token added since this file, unlike the spool limits, gates a real decision. The Rust adapter's `observe()` (`native/src/main.rs`) checks cheaply first -- Claude, `PreToolUse`, tool name `Agent` (confirmed from real transcripts; not `Task`), and an explicit `tool_input.model` present -- so every other hook stays on the unchanged, zero-cost path; a subagent spawned without an explicit override (the common case, resolved later from `.claude/agents/*.md` frontmatter or inherited) has no model visible here at all, an accepted, documented recall gap. Only when all of those hold does it read `policy/socket.json` and connect, with a strict `connect_timeout` plus read/write timeouts (150 ms each; a plain blocking `connect` against a stale port can retry for seconds on Windows, which `connect_timeout` avoids), asking the daemon a single JSON question (`cwd`, `session_id`, the alias in `tool_input.model`) and getting `allow`/`deny` back. The daemon resolves `cwd` through the registry (read-only; the adapter has not resolved projects since WD-027) and looks up the session's most recently *occurring* (`event_facts.occurred_at_us`, falling back to `received_at_us`) `agent_id IS NULL` Claude usage row's `model` from a read-only connection (`inspection.database`, not `Store`, to avoid contending with the poll loop's writer lock). The alias is only ever a model family name (`sonnet`/`opus`/`haiku`/`fable`) at this point, never the coordinator's full resolved id (e.g. `claude-opus-5-5`), so the comparison (`agent_watchdog.analysis.model_family_matches`) matches at the hyphen-delimited family segment, not exact equality. A candidate already at the lowest known tier (`haiku`) is never denied either, regardless of a match: the deny reason tells the caller to downgrade, and there is nothing lower to downgrade a `haiku` spawn to. Anthropic ranks `opus > sonnet > haiku`; the fourth alias, `fable`, has no established public rank relative to the other three and is deliberately left out of this floor rather than guessed (`agent_watchdog.daemon._LOWEST_TIER_ALIASES`). On `deny`, the adapter prints the verified, currently-supported Claude contract, `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`, on stdout with exit 0 -- the one path where the Claude adapter ever writes meaningful stdout, since every other Claude path stays silent (hook stdout otherwise reaches the model's context on `SessionStart`/`UserPromptSubmit`). Every failure -- missing/stale/malformed socket file, refused or timed-out connection, an unresolved project, no database, no observed coordinator model, a mismatched token -- resolves to `allow` independently; nothing here fails closed, matching "Watchdog failures must not stop the harness." The event is still spooled unconditionally on every path (the `log` half of `both` always fires) regardless of the decision. The socket is torn down, and its discovery file removed, on every daemon exit path (pause included) via the same `ExitStack` that owns the daemon lock, so a stale file left by an unclean exit is, at worst, a dead port a later process might reuse -- caught by the token check, never a false deny. Verified end to end with a real adapter binary against a real running daemon (`tests/test_rust_adapter.py::test_native_deny_round_trips_through_a_real_running_daemon`), not only fakes on each side; a live probe against a real Claude Code session (confirming the Agent tool's actual `PreToolUse` shape and that Claude Code honors the deny response) remains separate, opt-in live evidence, unverified until run.

**Amendment (WD-140, decision channel):** the policy socket above is now a generic decision channel, and it supersedes the wire details in the `intervene` paragraph. It does not change the rule's behavior or its fail-open guarantee.
- **Discovery.** `policy/socket.json` is schema 2: `{port, token, timeout_ms, max_request_bytes, subscriptions}`. The adapter connects only for a hook that matches a subscription (`provider`, `hook_event_name`, optional `tool_name`); since WD-142 the subscriptions are derived from the enabled, approved rules (`docs/rules.md`) and republished at the next poll tick when that set changes (it was the static `PreToolUse`/`Agent` cell before). The adapter's pre-filter on `tool_input.model` is gone: a model-less `Agent` spawn now costs one loopback round trip and the rule answers `allow`. Every other hook costs one bounded file read and never connects ([A/B evidence](evidence/wd140-ab-bench.json)).
- **Wire.** The request carries the complete hook input (`{schema_version: 2, token, provider, event_id, input}`), not three extracted fields, and the answer is an action: `allow|deny|ask|rewrite|context|block`. The adapter waits `timeout_ms` (300 ms, not 150 ms; clamped to 1500 ms against the fixed two-second hook timeout) inside one deadline covering connect, write and read.
- **Rules.** `agent_watchdog.rules.engine.decide` hosts the rules and returns the first non-`allow` action the adapter can render for that provider and event (`agent_watchdog.rules.api`). The same-model rule moved there from `daemon._resolve_policy_decision` unchanged, as `subagent_same_model`; `_LOWEST_TIER_ALIASES` moved with it. Codex got `allow` in every case until WD-151 added its exact CLI profiles.
- **Rendering.** The adapter's `render` is per provider and covers only the Claude cells WD-139 confirmed live. `allow` is silence, never `permissionDecision: "allow"` (which would auto-approve the tool); a `rewrite` carries `"allow"` with the full `updatedInput` and therefore also approves the call. A `Stop` or `SubagentStop` with `stop_hook_active` is never asked about and never blocked. The table is in `docs/hooks.md`.
- **Control events.** Each non-`allow` action sent is recorded as a `control` event (`source = "daemon"`), published as a prepared envelope to the shared spool from the handler thread. "Delivered" means "sent": the daemon cannot see whether the adapter rendered the answer in time.
- **Not changed (as of WD-140; Codex rendering was later added by WD-151, see its amendment below).** No rule beyond the move, no Codex rendering, no spool-format change. A schema 1 adapter against a schema 2 daemon (or the reverse) fails open, so the deny is silently lost until both are upgraded.

**Amendment (WD-141, session state):** the daemon keeps the state of every conversation agent in memory and in `session_state` (schema v8, [storage](storage.md#schema-v8-session-state-and-materialized-findings)). `Store.put` folds each event into it with the pure `state.apply`, in the event's own transaction; the poll loop hands the changed states to a `_SessionTracker`, which the decision-channel threads read without a database query. A rule receives the calling agent's state as `Context.session` (`agent_watchdog.rules.api.Context`), or `None` when nothing was observed for that agent: unknown is not zero, and a rule must treat `None` as no opinion. State holds counters and hashes only, never prompt, command, or output text. WD-142 moved `subagent_same_model` onto `Context.session.coordinator_model`.

**Amendment (WD-142, declarative rules):** the hard-coded rule is replaced by data. A rule is a TOML file with a closed set of predicates over the hook input and the session state and a fixed action vocabulary (`log|context|ask|rewrite|deny|block`), and executes nothing. Four ship with the package (`subagent_same_model`, `destructive_command`, `repeat_same_input_same_output`, and `stop_without_verification`, which is off until `rules enable`); user rules live in `<config-dir>/rules/` and run only while the SHA-256 of their file equals the digest the user approved (`rules approve`), so an edit makes them `stale` and inert. The engine resolves the project only after a rule has fired, applies two independent kill switches (`[defaults] policy_intervene`, which no project can override, and the per-project `policy_intervene`), enforces cooldowns and per-turn caps in memory, and records each action as a `control` event whose finding carries `rule`, `rule_version`, `action`, `evidence_ids`, `delivered_at` and the text sent. `analysis.control_findings` projects those events into findings, so `verdict`, `calibrate.py annotate` and `rules stats` treat them like any other finding; they carry an `action` and therefore never enter the M3 precision gate. Behaviour changes from WD-022b: the same-model rule now rewrites the subagent's model one tier down instead of denying (WD-139 confirmed `updatedInput.model` on Claude CLI), and reads the coordinator model from the daemon's state, so a spawn made by a subagent, whose own state has none, is not evaluated. Format, lifecycle, limits and CLI: [rules.md](rules.md).

**Amendment (WD-151, Codex CLI control):** approved declarative rules may now target the exact Windows CLI event/tool/action cells in `rules.api.CODEX_CELLS`. The native invocation explicitly selects `--codex-control-profile`; the daemon receives it separately from raw hook input and both sides enforce the gate. Unknown versions, modes, surfaces and missing profiles inherit no CLI permissions. Wildcard tool subscriptions narrow to probed cells. Unsupported matched requests record a bounded log-only capability gap with no delivery timestamp. The shared CLI/desktop installation selects no CLI profile; desktop activation remains gated by WD-153. All 12 cells have offline native/daemon round trips; production-adapter live acceptance currently covers CLI 0.162.0-alpha.17.2 `PreToolUse`/`apply_patch` deny only. See [WD-151 evidence](evidence/wd151.md) for scope, persistence, provider effects, and verification.

(WD-022b text, mechanism since superseded by WD-142 above; the per-rule flag described below was removed by WD-171; `rules disable subagent_same_model` turns that rule off by name.) This rule ships enabled by default, matching the WD-014 decision text ("adding a new deterministic policy rule is a config/implementation change" -- deploying this code is what enables it, the same pattern already used for the `log` half). At WD-022b it had its own kill switch, `Limits.policy_intervene_same_model_subagent_spawn`, checked before the database was touched; WD-142 kept it as an alias and WD-171 removed it, so the rule is now switched off by name (`rules disable`) or, with every other rule, by `policy_intervene`. That switch never gated the rule's own `log` half, which stays unconditional per WD-014. `daemon pause` (which also tears down the policy socket via the same `ExitStack`) still stops everything, including this rule, as a coarser fallback. Two further, accepted gaps beyond the explicit-model-override recall gap already noted: a spawn during a session's very first coordinator response has no coordinator usage row yet (transcript enrichment runs on the daemon's poll cadence, and the `claude-transcript-v1` reader holds a response open until its closing `stop_reason` before emitting it), so it is allowed, not evaluated; and this always-on behavior has not yet been applied to this project's own user-local live instance (`LIVE.md`) pending explicit sign-off, since it changes real tool-call outcomes and its live probe is still unverified.

## Rules and decision channel

The daemon decides which control action a hook returns. WD-140 built the channel (the amendment in "M4 LLM/control design" above) and WD-142 added the first rule layer: declarative TOML rules, four built-ins, hash approval and the `rules` CLI, described in [rules.md](rules.md). WD-143 adds approved-byte Python execution, cooperative timeouts and offline replay ([evidence](evidence/wd143.md)). Model-synthesized rule candidates remain planned in [TODO.md](../TODO.md) under WD-144; they become executable only after `rules approve`. The single extension point is a daemon rule with the fixed contract `evaluate(ctx) -> Decision`; there are no other plugins or dynamic loading. Every action is a recorded finding with `action`, `rule`, `evidence_ids`, `delivered_at`, and the text sent to the model, and the control invariant in TENETS.md governs all of it.

### WD-143 Python rule execution decision (2026-10-18)

**Implemented 2026-10-18; accepted by the user.** Python rules use supervised
cooperative worker threads under a trusted, debugged and tested code operating
model. This supersedes WD-143's former promise that arbitrary hanging rules cannot
affect the daemon or exceed a forced execution deadline. The user accepts diagnosis,
code correction and daemon restart as recovery from a hung rule. Startup
measurements and the discussion are in the [review record](evidence/wd143-review.md).

- Hash approval binds execution to the exact approved byte snapshot, including
  module initialization; execution never reopens the pathname to obtain code.
- `timeout_ms` bounds supervisor waiting for initialization and evaluation while
  the supervisor remains responsive. It does not terminate the worker thread.
  On timeout, attempt a diagnostic, ignore late results, return no control action,
  and disable the rule until daemon restart. Exceptions fail open and mark the
  rule degraded.
- Worker concurrency is finite, with at most one in-flight evaluation per rule.
  A timed-out worker that still runs occupies its slot; do not replace it with
  another thread or queue unbounded work. Unavailable capacity fails open.
- A rule update does not remove a hung thread or clear its disabled-until-restart
  state. Recovery requires daemon restart; changed bytes still need fresh approval.
- Threads share process memory and locks. Approval is not a sandbox, arbitrary
  rule code is not isolated from daemon failure, and forced thread termination
  is not part of the contract. Existing hook-channel failures remain fail-open.

The original design review changed only the specification. WD-143 now implements
this contract with approval, separate Python enablement and offline replay; see
the [implementation evidence](evidence/wd143.md). TENETS.md is unchanged, and no
rule approval or live model call is implied. WD-144's proposed-rule lifecycle
remains separately specified.

### WD-144 candidate approval and replay decision (2026-10-18)

**Accepted by the user; planned, not implemented.** Model-generated candidates
follow `rules approve` -> replay with `rules test` -> separate `rules enable`.
The user selected one code approval, with activation kept separate, rather than
distinct replay and live approval scopes. See the [review record](evidence/wd144-review.md).

- Generation, provenance recording, TOML validation and Python syntax compilation
  do not import or execute proposed Python rules. Candidates remain inactive.
- `rules approve` authorizes the exact reviewed bytes before any Python replay;
  the candidate remains inactive in the live daemon after approval.
- Replay executes approved bytes through WD-143's cooperative-thread contract
  against saved evidence, reports hypothetical decisions, and never delivers
  those actions to live provider sessions or activates the candidate.
- A separate explicit `rules enable` activates the approved candidate for normal
  evaluation. Unchanged bytes need no second code approval. Changes invalidate
  approval; neither replay nor enablement may bypass that check.
- Existing action/capability, kill-switch and promotion gates still apply. Code
  approval and replay are not measured rule precision or promotion authorization.
- Implementation format (`toml|python`) is stored separately from the existing
  deterministic/statistical signal classification. The model proposes the signal
  classification; the user explicitly confirms it at `rules approve`, and the
  approval record retains that confirmed classification. TOML is not inherently
  deterministic, and Python is not inherently statistical. Deterministic means
  enforcement of a specified policy, rather than merely repeatable code output;
  statistical means an inference from behavioral evidence. A model's label alone
  never grants approval, activation or stronger intervention. WD-147's promotion
  policy is defined in the separate WD-147 decision below. Candidate metadata edits cannot silently
  overwrite the human-confirmed classification in an existing approval record;
  changing that classification requires explicit human review and a recorded update.

This workflow targets generated candidates; it does not describe a change already
made to the current declarative-rule commands. The user also accepted the separate
format and human-confirmed classification contract on 2026-10-18. TENETS.md is
unchanged, and these decisions approve no rule body or live model call.

### WD-145 semantic judge review (2026-10-18)

**Accepted by the user; planned, not implemented.** Specification review is
resolved; implementation and live acceptance remain open. The
[review record](evidence/wd145-review.md) records the selected alternatives.

- Judge analysis is disabled by default. Explicit project opt-in and configured
  diagnostic triggers may request bounded analysis without first calibrating or
  promoting the originating statistical signal. Analysis is separate from the
  authority to send context or stronger control actions. This is the independent
  judge-specific diagnostic trigger decision anticipated by the historical M4
  re-opening clause; it does not open the M3 guidance/promotion gate.
- Results default to log/status only. Sending `context` needs separate explicit
  configuration, an approved enabled delivery rule, current global/project kill
  switches, and a confirmed provider/surface/version/event capability. LLM output
  itself is never an authorized control decision. An unsupported event leaves
  the result in log/status; never silently replace context with block or another
  action. Any stronger action needs its own explicit rule and applicable gates.
  An explicitly configured Stop block additionally requires confirmed Stop/block
  capability and `stop_hook_active` to be false; unknown or active state fails open.
- Every comparable user preference must be exposed in configuration, with
  validated defaults and project overrides: trigger selection and thresholds,
  backend/model/effort, timeout, daily call budget/accounting/day boundary,
  cooldown, delivery mode, pending-result lifetime/scope and pause behavior. Effective settings
  and reasons for unavailable or suppressed behavior must be inspectable in
  `judge status`. Configured preferences cannot waive approval, confirmed
  capabilities, kill switches, hard budget enforcement or fail-open invariants.
- Reuse the selected analysis backend independently of the observed session's
  provider. The Codex backend remains unavailable until WD-166 establishes its
  isolation/execution contract; no silent provider fallback is allowed.
- Pause or judge disablement defaults to cancelling the active analysis. Terminate
  only the provider CLI processes/descendants owned by that call, record
  `cancelled`, and complete owned-process cleanup before deleting temporary,
  scratch or authentication material. Already-started calls count toward the
  budget. Local CLI cancellation does not promise cancellation of remote work.
  The configurable alternative may finish within its timeout, retaining log/status
  only. Both modes stop new launches and delivery while paused or disabled;
  late output cannot authorize an action.
- Pending results may cross turns and, after the source session ends, move to a
  new session of the same project while configured validity permits. Never move
  a result to another project or treat a source session ending as an implicit
  expiry. Preserve source project/session/turn, evidence fingerprint, rule
  version/body and completion time; record the target session/turn separately.
  TTL starts at result completion. Apply configured lifetime/scope, and recheck
  source evidence relevance in the target context, current rule approval and
  enablement, current global/project opt-ins and kill switches, and the target
  provider/surface/version/event capability. Stale or unverifiable relevance
  leaves log/status only. Config may narrow scope to the source session/turn.
  A longer lifetime does not waive these checks.
- Reserve a call slot durably before starting the analysis invocation. Charge
  started or uncertain attempts, including failure, timeout and cancellation;
  refund only with proof that the analysis CLI never started. A configurable
  stricter mode may charge every reservation. Preflight refusal with proof of
  no analysis launch can therefore refund under the default mode. Crash-unknown
  reservations stay charged, with no automatic retry. Persist trigger/evidence
  deduplication and per-session cooldown; duplicate events and daemon restart
  must not recreate the same call. Use a documented configurable daily boundary;
  rollover or config reload must not erase charged history or allow outstanding
  reservations to bypass the applicable launch-day cap. Test these transitions.
- Persist delivery-attempt identity and suppress duplicate sends after replay or
  restart. Uncertain delivery acknowledgment is not permission to send again or
  proof of provider effect. Record selection, sent response, adapter rendering
  and observed provider effect separately as in WD-168. Cross-session delivery
  consumes an existing result, not a fresh analysis call or a per-session copy.
- Preserve structured analysis and `suggested_*` annotation suggestions with
  evidence and provenance; these do not replace human verdicts. Actual delivery
  remains a separately recorded rule-authorized control action.

Accepted starting defaults, all configurable:

| Preference | Default |
|---|---|
| Automatic judge enabled | `false` |
| Result delivery | log/status only; context opt-in |
| Per-call timeout | 120 seconds |
| Per-project daily call cap | 5 |
| Per-session cooldown | 600 seconds |
| Pending-result validity from completion | 1800 seconds |
| Pending-result scope | same project, including a new session after source end |
| Pause/disable behavior | cancel owned active analysis |
| Budget accounting | charge started/unknown; refund proven nonlaunch |

Keep explicit backend/model/effort selection independent of observed provider;
an unavailable selection stays unavailable. Configuration names, validation,
readers, defaults, project overrides and examples must ship together. Do not add
unsupported keys to `config.example.toml` just to document the future design.
Use one bounded worker and bounded admission, never an unbounded retry queue.
Implementation must preserve optional assistant-text capture under
`capture_content` and the planned `insights_redact = true` compatibility default.
Tests/CI remain provider-free; live acceptance is separately authorized. This
review does not deploy, enable or launch a judge, approve any rule body, change
TENETS.md. WD-147's promotion policy is defined in the separate decision below.

### WD-147 calibration and promotion review (2026-10-18)

**Accepted by the user; planned, not implemented.** Specification review is resolved;
implementation, dependencies and acceptance remain open. The
[review record](evidence/wd147-review.md) records the selected alternatives.

- Threshold enforcement defaults to advisory. `rules stats` shows the relevant
  reviewed count, precision, eligibility and risks. A human may explicitly confirm
  manual promotion despite insufficient sample size or precision. Record the
  eligibility shortfall, evaluated evidence scope and human override with the
  promotion event. Insufficient evidence stays insufficient, not a passing gate.
- A configurable strict mode refuses promotion below the configured threshold.
  Thresholds and mode are user preferences, with validated configuration and
  inspectable effective settings. Neither mode automatically promotes a rule.
- For actual runtime delivery, advisory mode does not waive exact-body approval,
  current rule enablement, project opt-in, global/project kill switches or
  confirmed target capabilities. A promotion grant may be recorded for a disabled
  candidate, but never acts as implicit code approval or enables that candidate.
- Reuse WD-144's separate format/classification and human confirmation contract.
  Scope reviewed data and recommendations to provider, rule version and body hash;
  historical precision must never be represented as evidence for changed code.
- Store intervention grants outside executable rule bytes, bound to the provider,
  rule version and approved body hash, with a durable promotion/demotion history.
  A grant change does not rewrite code or require fresh approval of unchanged
  code. Grants may select only actions/payloads supported by the approved rule;
  they do not invent a decision the rule did not authorize.
- Changed code is inert until fresh hash approval and inherits no old grant or
  precision. At reapproval, offer restoration of the prior level through a
  separate explicit confirmation. Record restoration as a manual promotion for
  the new body, disclose its empty/insufficient sample and respect the configured
  advisory/strict mode. Code approval alone never restores the grant. Without
  confirmed restoration or a later promotion, the new body has no permission
  above log; any disabled candidate still needs separate enablement. Historical
  grants/verdicts remain inspectable under their original version/body/provider.
- The default sample unit is a unique human-reviewed finding, counted once per
  evidence fingerprint within provider/version/body scope. Show distinct session
  counts separately to disclose concentrated samples. Precision is
  `TP / (TP + FP)`; uncertain, unreviewed and unconfirmed model suggestions do
  not enter either that denominator or the reviewed minimum. No reviewed data
  means unavailable precision, not zero or perfect precision. Disclose cohort,
  review coverage and denominator. Reannotations update one authoritative human
  verdict with history, not an extra observation. The sample unit is configurable;
  session aggregation may be selected explicitly, with partial sessions excluded
  from its qualifying minimum and any confirmed FP making a complete session FP.
- If later reviewed precision falls below the current level's threshold, default
  to a warning and recommended manual demotion; retain the level until human
  action. This applies even when strict mode governs new promotion requests.
  Precision-drop reaction is separately configurable: an explicitly selected
  automatic-demotion mode may lower to the strongest currently qualifying level,
  or log if none qualifies, and records the change. Recovery never automatically
  raises the level. Approval revocation, stale bodies and kill switches are not
  statistical preferences and still take effect immediately.

Accepted starting eligibility criteria, all configurable. Statistical percentages
use confirmed `TP / (TP + FP)` and the reviewed minimum; deterministic strong-action
criteria require confirmed positive cases, not merely unreviewed firings without
an FP label. The default unit is a unique reviewed finding, as defined above.

| Action | Statistical rule | Human-confirmed deterministic rule |
|---|---|---|
| log | No minimum | No minimum |
| context | >=60%, at least 10 reviewed cases | No calibration minimum |
| ask | >=80%, at least 20 reviewed cases | At least 10 TP, 0 FP |
| rewrite | >=90%, at least 30 reviewed cases | At least 10 TP, 0 FP |
| deny | >=90%, at least 30 reviewed cases | At least 10 TP, 0 FP |
| block | >=90%, at least 30 reviewed cases | At least 10 TP, 0 FP |

These values describe eligibility/recommendations in advisory mode and enforced
criteria for new promotions in strict mode. Deterministic exemptions do not
approve, enable or promote a rule, or override provider/kill-switch checks.
Sharing an eligibility threshold does not make rewrite/deny/block interchangeable;
select only actions the approved rule supports. Configurable automatic-demotion
uses a declared supported fallback order, ending at log, without inventing payloads.

Prefill defaults to per-case human confirmation: show the case, evidence and
suggested TP/FP/uncertain; the user confirms or corrects it. Suggestions remain
`suggested` until that action, preserve analysis provenance and evidence identity,
and never overwrite a human verdict. A human-confirmed uncertain case remains
uncertain and does not increase TP/FP or the qualifying minimum. A configurable
batch mode requires explicit case selection and confirmation with recorded batch
membership and provenance; it never silently accepts every suggestion.
Suggestions for another provider/version/body/fingerprint cannot label the current
case. Reuse saved suggestions where available; fresh model analysis must be an
explicitly requested invocation through the selected backend, with no fallback
when backend/isolation is unavailable. This design review launches no such call.

Expose enforcement mode, per-action thresholds, deterministic exemptions, sample
unit, precision-drop reaction and confirmation mode through validated config with
defaults/project overrides and inspectable effective values. Record promotion,
demotion, changed-body restoration and advisory override confirmations durably,
including evaluated evidence scope, approved body, eligibility and provenance.
Keep recomputation/invalidation aligned with WD-162 and Codex evidence with WD-167.
Reports must separate raw observations, unique reviewed cases, session diversity,
uncertain/unreviewed counts, precision denominator and delivery-stage coverage;
human correctness verdicts do not prove provider effect or task success.

This decision changes the planned promotion policy, not runtime state. It does
not promote or enable any rule, approve a code body or authorize a model call.
Implementation and existing task dependencies/acceptance stay open. Historical
cohort measurements and selected rules remain as recorded; this review neither
relabels those cohorts nor changes TENETS.md.

## Current versus superseded decisions (WD-169)

Historical sections stay in place as the record of what was decided and why. This
table says which text governs today. Open status and dependencies live in
[TODO.md](../TODO.md); this table records no task as complete that TODO.md keeps open.

| Topic | Governing text | Superseded or historical text |
|---|---|---|
| Delivery scope and control surface | "Boundaries" and the [compatibility matrix](provider-compatibility.md): per-cell live evidence; Codex CLI by exact WD-151 profile; Codex desktop unprobed (WD-153) | Claude-first and "Codex always silent" wording in the WD-022b/WD-140 amendments, kept as history |
| Hook control invariant | TENETS.md (WD-148): action only from an approved rule through the decision channel, fail-open, recorded | The "narrow exception to an observation-only invariant" in the WD-014 text |
| Rule execution | WD-142 declarative rules; WD-143 supervised Python rules (implemented) | WD-143's former forced-deadline promise |
| Rule synthesis (WD-144) | [WD-144 decision](#wd-144-candidate-approval-and-replay-decision-2026-10-18): accepted, not implemented | None |
| Semantic judge (WD-145) | [WD-145 decision](#wd-145-semantic-judge-review-2026-10-18): accepted, not implemented | WD-014 calibrated-trigger requirement, the 60 s timeout, finish-in-flight-only and context-by-default delivery, for judge analysis only |
| Promotion ladder (WD-147) | [WD-147 decision](#wd-147-calibration-and-promotion-review-2026-10-18): accepted, not implemented; advisory by default | Mandatory 90% gate and "no `wd-010.v1` rule selected" in "M3 guidance policy", kept as the measured WD-012/WD-122 record |
| Analysis backend | "Backend selection (WD-166, partial)": Claude default; Codex returns `isolation_unavailable` ([WD-166a](evidence/wd166a.md)); WD-166b/c blocked | WD-123's `claude -p`-only description, accurate for that path |
| Delivery stages | WD-168: selected, response sent, adapter rendered and provider effect are separate; `delivered_at` means sent | The WD-140 "delivered means sent" note is consistent; no record upgrades it to an effect |
| Codex attribution and execution | WD-164 execution projection and WD-165 turn-context attribution | The note that Codex usage lacks per-subagent model attribution |
| Statistical rules | `wd-010.v2` (WD-146); v2 precision unmeasured until user verdicts | `wd-010.v1` findings and the WD-012/WD-122 tables, as measured |
| Codex App Server | WD-015: no generic attach contract; no adapter | None |
