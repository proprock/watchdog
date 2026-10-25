# Observation hooks

WD-006 supports the native hook adapter for Codex CLI and local desktop coding
sessions. WD-022a adds Claude Code CLI and desktop Code as a second provider;
WD-022b added Claude transcript enrichment and usage reconciliation. By default the
adapter observes only: it never invokes a model and changes harness behavior only
when the daemon's decision channel returns an action for an approved rule on a
live-confirmed cell (see "Adapter behavior" below and the
[compatibility matrix](provider-compatibility.md)). Ordinary chats and Cowork are
out of scope.

The provider is a required positional: `agent-watchdog hook codex|claude` and
`agent-watchdog hooks install|uninstall codex|claude`. The two providers share
the envelope schema, the daemon API, locks, loss counters, redaction, and the
durable schema 2 JSON spool; only the native-event map, the target file, and the no-op
output differ.

## Register and install

Use an installed Watchdog environment with a stable Python executable location.
Generated commands use that interpreter and absolute Watchdog paths, so moving
or deleting the environment requires uninstalling and reinstalling the hooks.

Explicitly register the repository before installing observation hooks, unless
the opt-in trusted Git auto-add configuration below applies:

```console
agent-watchdog project add /absolute/project
```

To collect any new Git checkout below one trusted directory, configure both
options. `~` expands to the local home directory and is saved as a canonical
absolute path. The directory must already exist. Auto-add never registers
non-Git directories or paths outside that directory.

```toml
auto_add_projects = true
trusted_projects_dir = "~/repos"
```

Choose an explicit absolute `hooks.json` path. Use a project-local
`<project>/.codex/hooks.json` for one checkout, or a user-level file for registered
repositories and worktrees. Watchdog still checks the registry for every event.
Do not install the same observer at both levels: Codex combines hook sources.

Installation always requires a native adapter (WD-110): omitting both
`--adapter-executable` and `--adapter-artifact` on `install` is rejected with a
clear error, never silently installed as the Python fallback described below.

For a release-built Rust adapter, download the ZIP matching the current host and
verify its entry in `SHA256SUMS.txt`, then pass
`--adapter-artifact /absolute/download/agent-watchdog-hook-v<VERSION>-<RUST-TARGET>.zip`.
The installer validates the archive's manifest, checksum, filename, and current
OS/architecture, then copies its binary below the Watchdog data directory before
writing the hook. It invokes no Rust toolchain; an unsupported host or another
target's archive is rejected with an explicit diagnostic. The declared targets
are `x86_64-pc-windows-msvc`, `x86_64-unknown-linux-gnu`, and
`aarch64-apple-darwin`. Intel macOS is not a declared target: GitHub no longer
offers standalone Intel-hosted macOS runners, and Apple Silicon has long
since replaced Intel Macs.

For a locally built adapter, instead pass
`--adapter-executable /absolute/bin/agent-watchdog-hook` (`.exe` on Windows).

```console
agent-watchdog hooks install codex --file /absolute/project/.codex/hooks.json --adapter-artifact /absolute/download/agent-watchdog-hook-v<VERSION>-<RUST-TARGET>.zip
agent-watchdog hooks install codex --file /absolute/project/.codex/hooks.json --adapter-artifact /absolute/download/agent-watchdog-hook-v<VERSION>-<RUST-TARGET>.zip --apply
agent-watchdog hooks uninstall codex --file /absolute/project/.codex/hooks.json
agent-watchdog hooks uninstall codex --file /absolute/project/.codex/hooks.json --apply
```

The generated command also records the current Python executable, used only to
start a missing core. Switching adapters requires uninstalling the recorded
installation first, reinstalling, and reviewing the new definitions through
Codex. Uninstall uses the ownership record and works even if the native binary
has been removed; omit both adapter options when uninstalling — uninstall
never requires one, since it only removes the previously recorded group.
Release checksums are published, but this project has no artifact-signing
identity or signing policy yet, so a checksum is not a signature.

`agent-watchdog hook codex|claude` (singular, no `--adapter-*` options) reads
one event from stdin directly through the Python core — `hooks.py::observe` —
resolving the checkout and publishing a prepared envelope to the shared spool.
It is a developer/test utility only, never what
`hooks install` produces: production installation always requires a native
adapter, so an installed hook always invokes the separate `agent-watchdog-hook`
binary, never this Python entry point. See `## Adapter behavior` below for what
that binary actually does; do not confuse the two by name alone.

The native adapter shares the existing loss counters and atomic-write / lock
primitives, and durably spools each event as JSON under `<data>/spool/`. The
daemon resolves and builds those records, then commits directly to its per-project
SQLite Store alongside retention, analysis, and CLI duties. There is no second
event Inbox or intermediate durable envelope write.
Rust contract tests run through pytest after the release binary has been built.

Without `--apply`, commands return the proposed configuration and perform no
writes. `--home`, or individual `--config`, `--data`, and `--runtime` options, go
before `hooks` and select an isolated Watchdog installation. Installation does not
register a project, start collection, change inline TOML, or change native trust.

Open Codex `/hooks`, review the exact generated definitions, and trust them through
the product. Restart/resume the coding session after configuration changes. Project
trust and hook trust are separate native controls. Watchdog never edits trust
records or supplies a trust-bypass flag. Managed policy may still prevent hooks.
Review in the profile actually used by each surface: in the Windows probe,
`--profile lean` saved trust in `lean.config.toml`; desktop required a separate
native review without that profile. Do not copy trust hashes between files.
See the [official hook reference](https://learn.chatgpt.com/docs/hooks).

### Codex CLI control profile

Control-enabled Codex CLI installations use the native adapter option
`--codex-control-profile VALUE`. This value is installation-scoped and belongs
only in that CLI installation's native hook command. Configure it only after
confirming the executable and mode for that installation. Do not add a CLI profile to a
shared CLI-and-desktop command or a global hook source; desktop has its own profile below. Confirm the running executable's exact version and invocation
mode before selecting a profile. A newer version, a payload's `provider_version`,
or its claimed surface does not inherit a profile. Unknown or missing profiles
remain observation-only.

| Profile | Permitted controls |
|---|---|
| `windows-cli-0.160.0` | `SessionStart` and `UserPromptSubmit` context; `PostToolUse`/`Bash` context; `PreToolUse`/`Bash` deny or rewrite; `SubagentStop` block |
| `windows-cli-exec-0.162.0-alpha.17.2` | `PreToolUse`/`Bash` context; `PreToolUse`/`apply_patch` deny or context; `SubagentStart` context; `Stop` block |
| `windows-cli-interactive-0.162.0-alpha.17.2` | `PreToolUse`/`apply_patch` rewrite only; the evidence is an operator-approved scratch edit |
| `windows-desktop-26.1016.62342-0.163.0-alpha.11.2` | `SessionStart` context; `PreToolUse`/`Bash` and `PreToolUse`/`apply_patch` deny or rewrite; `PermissionRequest`/`Bash` deny or permit; `PostToolUse`/`Bash` block; `Stop` and `SubagentStop` block |
| `windows-desktop-26.1021.31345-0.164.0-alpha.11.1` (provisional) | The same cells as the profile above, limited to `context`, `deny` and `block`; never `rewrite` or `permit` |

A provisional profile names an app/backend pair no provider probe has covered. It
acts at once, because a `deny`, `block` or `context` that is not honoured only
leaves the harness as if Watchdog were absent. It is demoted to log-only (finding
gap `profile_demoted`, record `profile-demotions.json` in the data directory) the
first time a call that the daemon denied under it is seen to finish. Delete that
record only after a probe has confirmed the profile.

The desktop profile names the exact app and backend pair that WD-153 probed
and belongs only in a desktop installation's native hook command; the CLI
profiles above never apply to it, nor it to them. The operator confirms the app
version (About) and the backend before selecting it. Prompt, `PreToolUse` and
`PostToolUse` context, `ask`, `SubagentStart` and compaction events stay
observation-only there because their probes were unsupported or inconclusive.

The profile is an exact allowlist, not a general compatibility claim. For an
unsupported matched request the hook stays fail-open and records only the bounded
`unsupported_codex_cell` capability gap described in [rules.md](rules.md#minimal-codex-rule-and-approval).
Only the desktop profile enables `PermissionRequest`; the CLI profiles and the named
MCP fixture do not. The desktop deny renders
`hookSpecificOutput.decision = {behavior: "deny", message}` and `permit` renders
`{behavior: "allow"}`, the shapes WD-153 confirmed. Codex installations observe
`PermissionRequest` as a `waiting` event; an installation made before that
(readiness reason `hook_events_outdated`) is completed by rerunning the hook install
command, and can still be uninstalled unchanged.

## Ownership and recovery

The installer adds one separate matcher group for each of 11 observed Codex events.
It retains existing groups and unrelated JSON fields. Repeated installation with
the same paths makes no change. Commands include an installation UUID used only
for ownership; it is not recorded as a native event identity.

Sibling files are local installation records, not additional hook sources:

- `hooks.json.watchdog.json` records exact owned groups and original text.
- `hooks.json.watchdog-backup-<sha256>.json` preserves pre-edit bytes.
- `hooks.json.watchdog.lock` serializes cooperating installer processes.

Ownership is saved before the hook file, so a failed write can be retried or
uninstalled. Uninstall removes only exact owned groups and preserves later user
additions. If nothing else changed, it restores original bytes or removes the
newly created file. Edited owned groups, missing ownership records for detected
Watchdog hooks, invalid JSON, duplicate JSON keys, symlink files, changed paths,
and conflicting backups are refused. Backups and the lock file remain after
uninstall; they may contain original user hook commands, so keep them local.

Configuration is capped at 1 MiB. A final comparison catches edits made during
preparation, but the lock cannot serialize an unrelated editor's writes. Avoid
editing hooks concurrently with installation. This is a local file utility,
not a transactional configuration manager.

## Adapter behavior

Since WD-027/107 the native adapter (the separate `agent-watchdog-hook` binary
that `hooks install` always wires up — not the Python CLI's `hook` subcommand
described above) is spool-and-forget. Invoked with `<codex|claude>` as its own
positional, it reads JSON stdin and durably writes the complete raw provider
input plus raw `cwd`, an
adapter-stamped `event_id`, and `received_at` under `<data>/spool/`. `cwd` is kept
only for checkout resolution. The adapter does not maintain a semantic provider
field allowlist or normalize provider telemetry. Records carry `schema_version: 2`
and `record_type: "hook"`; prepared developer/control records use
`record_type: "envelope"`. Schema 1 spool files have no runtime compatibility
reader; upgrading pending legacy data requires an external one-shot migration.

Checkout resolution, envelope construction, per-project payload/project quota,
unknown-field warnings, and SQLite admission all run in the daemon's spool
drain, once per poll. A newly seen top-level field is retained in provider metadata
and emits one bounded WARNING containing the provider and safe field name, never
its value.
The drain resolves the spooled `cwd` from filesystem reads — the nearest `.git`
entry, a worktree `.git` file's `gitdir:` line, and a `commondir` file when
present — producing the same values as
`git rev-parse --path-format=absolute --show-toplevel --git-common-dir`; only an
unrecognized layout falls back to spawning `git` with its 250 ms subprocess
timeout. An unregistered or since-removed `cwd` discards the record, except an
unregistered Git checkout under the configured trusted directory is added and
then admitted. A resolution failure never stops the daemon (WD-170): a `cwd` that no longer
exists is discarded and logged as `decision=discarded reason=unresolvable_cwd`, while one that still
exists (for example after a Git timeout) stays for the next pass and is logged once as
`decision=deferred reason=unresolvable_cwd`. A busy
project writer leaves the record for the next poll; admission/diagnostic lock
waits are at most 100 ms.

The adapter reads only `<data>/spool/limits.json`, which the daemon publishes:
the largest registered payload limit and the spool cap. When it is absent the
adapter uses built-in defaults (1 MiB payload, 64 MiB / 4096-file spool). Raw
stdin is capped at that payload limit; over it increments the `payload` counter.
A full spool increments `quota`. Native and Python publishers share
`<data>/admission.lock` for queue occupancy and atomic publication, with a
100 ms lock wait. Prepared envelopes also pass the resolved project's canonical
payload-size check at drain. `event_id` / `received_at` are stamped at
arrival and trusted by the daemon, so a replayed spool record yields the same
event and the project store deduplicates it. Native handlers are synchronous
with a two-second timeout; the launch-cost measurements are not a p95 whole-hook
latency figure. WD-024 closed with recorded Windows shell-launch limitations; the
Codex hook-performance limit is recorded under WD-026: Codex fixes the hook shell (PowerShell), whose empty startup already costs ~280 ms sequential / ~550 ms four-way. WD-027 removed the per-hook
config parse, redaction-regex compile, and project-tree walks, cutting the
synthetic concurrent four-caller p95 from 96 ms to 65 ms
([evidence](evidence/wd027-spool-bench.json)). Do not treat zero loss counters as
proof that a provider invoked every hook.

**Decision channel (WD-140).** After the payload is parsed, the adapter reads
`<data>/policy/socket.json` (one bounded read, 16 KiB cap). Only when that file is
schema 2 and the hook matches one of its `subscriptions` — provider, event name,
and optionally tool name — does the adapter connect to the daemon's loopback
socket, send the complete hook input with the spooled `event_id`, and wait at
most the published `timeout_ms` (default 300 ms, clamped to 1500 ms so the
whole round trip stays inside the fixed two-second hook timeout). An
unsubscribed hook, or any hook while no daemon is publishing the file, never
connects; the only added cost is that one file read. Measured against the
previous adapter on the same host
([WD-140 A/B evidence](evidence/wd140-ab-bench.json)), the warm figures overlapped,
while that historical cold first call was about 5-8 ms slower. The WD-154
quiet-window re-run found current Codex unsubscribed p50 of 39.2-42.0 ms versus
38.9-42.6 ms for the same-host pre-channel baseline; even that old adapter did
not reproduce WD-027's 29.4 ms p50. Current-source normal/disabled-discovery
builds gave inconsistent cold differences (-35.8, -57.4, +12.7 ms), so a stable
5-8 ms discovery penalty was not reproduced. Retain the bounded read without a
cache or skip ([data, cold ranges and limitations](evidence/wd154.md)).

Subscribed `PreToolUse` measured 44.8-48.6 ms sequential p95 / 80.0-96.6 ms
four-caller p95 for Claude `Agent`, and 44.1-45.2 / 86.0-99.2 ms for the evidenced
Windows Codex CLI exec 0.162.0-alpha.17.2 `apply_patch` profile. Every native
callback reached the decision channel. Separate Python socket-client p95 was
16.2-26.1 / 12.4-21.9 ms for Claude and 15.8-17.8 / 2.9-5.4 ms for Codex, against
the published 300 ms budget. These are synthetic direct-adapter and separately
labeled socket measurements; they exclude actual provider-runner overhead and
do not establish desktop control support ([WD-154 evidence](evidence/wd154.md)).

A `Stop`/`SubagentStop` whose input carries
`stop_hook_active` (anything but an explicit `false`) is never asked about. The
event is spooled whatever the answer, and the answer is rendered after the
spool write is attempted, so a quota or I/O failure still delivers it.

A mismatch must not stay silent (WD-155). The adapter declares the schema it speaks in
`--version` (`agent-watchdog-hook 0.1.5 (providers: codex, claude; decision schema 2)`;
a build without that suffix predates the channel), and `readiness` fails its
`decision_channel` stage when that differs from the published file. `daemon status`
adds `"warnings": ["decision_channel_unused"]` when events a published subscription
covers have arrived since the daemon started and no adapter has asked about any, which
is what an adapter that cannot speak the schema looks like from the daemon's side. The
warning is evidence of silence, not proof of a mismatch, and `status.json` carries the
window it covers (`decision_channel.observed_since`). It stays absent when the daemon
is idle (no covered event arrived), when control is switched off (`policy_intervene =
false` publishes no subscriptions, so nothing counts), and for traffic no subscription
covers. Only `readiness` can name the two schemas.

Every failure — no file, another schema version, a stale port, a refused or
slow connection, a request over the published `max_request_bytes`, an empty,
oversized, malformed or unknown-action answer — means "no opinion" and prints
the normal default. Each path has a test in `tests/test_rust_adapter.py`. A
mismatched adapter and daemon (schema 1 against schema 2) therefore fails open;
upgrade both together.

Rendering is per provider and limited to the cells recorded in
[provider-compatibility.md](provider-compatibility.md). Claude uses the
confirmed cells listed there. Codex CLI control is additionally gated by the
installation-scoped `--codex-control-profile VALUE` on its native command; it is
never inferred from payload fields. Every other
combination, and `allow`, prints nothing:

| Event | Action | Claude stdout |
|---|---|---|
| `PreToolUse` | `deny`, `ask` | `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"\|"ask", "permissionDecisionReason": …}}` |
| `PreToolUse` | `rewrite` | same envelope with `"permissionDecision": "allow"` and `"updatedInput"` (the complete replacement `tool_input`). **A rewrite also approves the call**, bypassing the user's permission prompt for it. |
| `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `PreCompact` | `context` | `{"hookSpecificOutput": {"hookEventName": …, "additionalContext": …}}` |
| `PostToolUse`, `Stop`, `SubagentStop` | `block` | `{"decision": "block", "reason": …}` (`PreCompact` block is only weakly evidenced, so it is not rendered) |

`allow` is never rendered as `permissionDecision: "allow"`: that would
auto-approve the tool. The Codex CLI profile gate is exact-version and
installation-scoped. It does not apply to shared CLI-and-desktop hook commands;
Codex desktop remains unprobed and gated by WD-153. The daemon only answers with
actions renderable for the provider, event, tool and selected profile, so the
`control` events it records do not claim actions the adapter would drop. Native
rewrite uses `updatedInput.command` (a required string) and omits
`permissionDecision: "allow"`.

Normal completion and handled failures emit exactly `{}` (codex) or nothing
(claude) and exit zero, unless the decision channel answered a subscribed hook
as above. Missing-daemon recovery is detached and does not wait for
readiness. Persistent pause prevents spooling and restart. Invalid input,
registry/config errors, and spool-write failures are ignored by the hook. An
internal failure increments the `invalid` counter and appends one content-free
line — `<timestamp> <event-name> <reason-category>` — to a rotating, size-capped
`faults.log` under the data directory, so a nonzero counter is attributable.
Reason categories only (for example `cwd-relative`, `control-invalid`,
`io-error`); never prompt, path, command, or tool-output text. Missing Python or
a broken installation cannot be handled inside Python; repair the installation if
the product reports command-launch errors.

Metadata includes session/turn/agent identifiers, checkout identity, event kind,
tool-call ID/name, and tool-response type. Identifiers are bounded. WD-007 also
captures the native `prompt`, `tool_input`, `tool_response`, and
`last_assistant_message` fields under `payload.<provider>.content`, when present.
Capture defaults to true for registered projects; `capture_content = false` in
defaults or project overrides omits those fields at drain. Every other native
field, known or not, stays under `payload.<provider>.metadata` even with capture
disabled, including text-bearing fields such as Claude PostCompact
`compact_summary`; unrecognized names are also listed in `unknown_fields`.
`transcript_path` (and Claude `agent_transcript_path`) is additionally promoted to
`payload.<provider>` as a validated absolute path for the daemon's transcript
readers. Pre-WD-115, known credential forms are
removed in the adapter before the spool write — the first durable write — and the
daemon re-sanitizes when it builds the canonical envelope; WD-115 removes both
steps so the store keeps raw input and the credential filter runs only at
sanctioned export. Either way this is not an anonymization or complete
secret-detection claim. Quotas and persistent diagnostic counters are described
in [storage](storage.md).

CLI versus desktop and backend version remain unknown unless evidence supplies
them; the adapter does not guess from cwd or the installed CLI version. Tool-call
IDs are not event IDs. A PostToolUse string is not parsed as a universal exit code,
and Stop means turn end, not task success. Missing native fields remain null.

## Claude observation (WD-022a)

Register the repository first, then install into an explicit absolute
`settings.json` or `settings.local.json` — no other file name is accepted for
`claude`, and `hooks.json` is refused. Use a project-local
`<project>/.claude/settings.local.json` for one checkout or a user-level
`settings.json` for registered repositories and worktrees.

```console
agent-watchdog project add /absolute/project
agent-watchdog hooks install claude --file /absolute/project/.claude/settings.local.json
agent-watchdog hooks install claude --file /absolute/project/.claude/settings.local.json --apply
agent-watchdog hooks uninstall claude --file /absolute/project/.claude/settings.local.json --apply
```

**Exec form, no shell.** Each entry is `{"type": "command", "command": <abs
executable>, "args": [...], "timeout": 2}`. Claude resolves `command` as an
executable and spawns it directly with `args`, so paths containing quotes, `$`,
or backticks never reach a shell parser and no `shlex.join` or `commandWindows`
is needed. With `--adapter-executable`, `command` is the native binary and `args`
begin `--python <sys.executable> …`; without it, `command` is `sys.executable`
and `args` begin `-m agent_watchdog …`. There is no `matcher` (optional per
schema; absent matches all) and no `commandWindows` (Claude has no such field).

**`timeout: 2` is fixed** for the shared observation/control entries — the same
production budget as Codex. It is not parameterized and is not raised to pass
acceptance. WD-154 closes the separate-entry question: subscribed native calls
measured 44-49 ms sequential p95 and 80-99 ms four-caller p95; no separate entry
or longer timeout is needed. The adapter's decision wait remains 300 ms by
default and fail-open ([evidence and scope](evidence/wd154.md),
[architecture](architecture.md)). These are direct-adapter measurements,
excluding actual provider-runner overhead.

**Empty stdout is required, not stylistic.** Claude adds a hook's stdout to the
model context on `SessionStart` and `UserPromptSubmit`. Printing `{}` there would
inject model context and break the invariant that observation hooks do not
continue a turn or inject context. The adapter therefore prints **nothing** for
`claude` on every observation path — success, unregistered project, malformed
JSON, oversized payload, pause, and internal failure — and always exits 0. The
Rust adapter makes the same choice by scanning its raw arguments for the `claude`
token, so a parse failure is also silent. Codex still receives `{}`. The one
exception is the decision channel (WD-022b, generalized by WD-140): a Claude hook
the daemon has subscribed to, and answers with a renderable action, prints that
action's contract instead of staying silent (with the built-in rules: `PreToolUse`
on `Agent` and `Bash`, and `PostToolUse` on every tool, plus `Stop` once
`stop_without_verification` is enabled; user rules add their own). See "Decision channel"
above and "M4 LLM/control design" in `docs/architecture.md`.

**Twelve native events map to existing envelope kinds** (the `control` kind is
not hook-derived: the daemon records it, with `source = "daemon"`, for each
action its decision channel sends — see `docs/daemon.md`):

| Native | Kind | Native | Kind |
|---|---|---|---|
| `SessionStart` | `session.start` | `PostToolUse` | `tool.finish` |
| `SessionEnd` | `session.end` | `PostToolUseFailure` | `tool.finish` |
| `UserPromptSubmit` | `turn.start` | `PreCompact` | `compaction.start` |
| `Stop` | `turn.end` | `PostCompact` | `compaction.end` |
| `PreToolUse` | `tool.start` | `SubagentStart` | `agent.start` |
| `Notification` | `waiting` | `SubagentStop` | `agent.end` |

`PostToolUseFailure` maps to `tool.finish` with `availability.tool_outcome =
"unknown"`, identical to every other tool event; the failure signal survives only
as the native `hook_event_name` in `payload.claude`. No outcome parser is added —
tool success is not progress, and neither is its converse. **Claude has no
`Interrupt` event** in this build. An unknown native name yields kind `unknown`
with `payload.claude.hook_event_name = "unknown"`, preserved, not fabricated.
`turn_id` is not sent by Claude and stays null; it is not invented.

**Content capture is deliberately the four Codex-equivalent fields only** —
`prompt`, `tool_input`, `tool_response`, `last_assistant_message` under
`payload.claude.content`, subject to `capture_content`. Claude's additional
`error`, `duration_ms`, and `is_interrupt` fields are retained as
`payload.claude.metadata`, independent of `capture_content`, the same as every
other non-content telemetry field — they are scalar/boolean or (for `error`)
already-surfaced exit diagnostics, not the free-text conversational content
`capture_content` gates (WD-022b).

The ownership record adds `"provider"` at `schema_version` 1. A Codex manifest
written before this change has no such key and is read as `"codex"`, so an
existing Codex installation still uninstalls cleanly. Sibling files follow the
target name: `settings.json.watchdog.json`, `settings.json.watchdog-backup-<sha>`,
`settings.json.watchdog.lock` (and the `settings.local.json.*` equivalents).
Review the generated definitions through Claude `/hooks` and restart the session;
this command never changes trust.

## Verification

Offline pytest covers known synthetic Codex and Claude payload shapes, opaque and
structured tool responses, exact no-op output on failures (`{}` for Codex, empty
for Claude), input bounds, registry exclusion, persistent pause, shell quoting,
idempotent installation, original-file recovery, later user edits, edited owned
hooks, interrupted installation, cross-provider file/path mismatch, and a legacy
Codex manifest without `provider`. It never installs into an active provider
configuration or invokes a model.

Live Windows evidence and remaining limits are recorded in
[verification](verification.md#wd-022a-claude-observation-gate-met); the WD-022a
gate is met (interactive `durationMs` probe, four-concurrent `claude -p` Pass A
re-run with no dropped events and zero losses, and a supervised desktop pass
with a real composer submission). macOS/Linux live checks remain WD-019.

`scripts/hook_stream_timing.py` supports the live CLI pass: `capture` records a
stamped NDJSON of `hook_started`/`hook_response` and tool records from a headless
session (ids, counts, outcomes only — never prompt, command, or tool output), and
`report` (schema 2) emits `hook_pairs`, `unpaired_hook_started`, `per_event`,
`outcomes`, `nonempty_stdout_responses`, and `tool_use_ids`. It reports **no
timing delta**: the read-time gap between the two stream records is dominated by
Claude's stdout-flush cadence, not by hook wall time, so it is not a substitute
for a harness-reported duration. The tool is kept only for pairing, outcome, and
`tool_use_id` verification.
