# Observation hooks

WD-006 supports the native hook adapter for Codex CLI and local desktop coding
sessions. WD-022a adds Claude Code CLI and desktop Code as a second provider,
observation only. The adapter never changes harness behavior and never invokes a
model. Ordinary chats and Cowork are out of scope. Claude transcript enrichment,
usage reconciliation, and guidance/control delivery are deferred to WD-022b.

The provider is a required positional: `agent-watchdog hook codex|claude` and
`agent-watchdog hooks install|uninstall codex|claude`. The two providers share
the envelope schema, the daemon API, locks, loss counters, redaction, and the
durable JSON inbox; only the native-event map, the target file, and the no-op
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

```console
agent-watchdog hooks install codex --file /absolute/project/.codex/hooks.json
agent-watchdog hooks install codex --file /absolute/project/.codex/hooks.json --apply
agent-watchdog hooks uninstall codex --file /absolute/project/.codex/hooks.json
agent-watchdog hooks uninstall codex --file /absolute/project/.codex/hooks.json --apply
```

For the Rust adapter, add `--adapter-executable /absolute/bin/agent-watchdog-hook`
to both install commands (`.exe` on Windows). Build it using the commands in
[README](../README.md), then copy it to a stable location. The generated command
also records the current Python executable, used only to start a missing core.
Omitting this option selects the Python fallback. Switching adapters requires
uninstalling the recorded installation first, reinstalling, and reviewing the new
definitions through Codex. Uninstall uses the ownership record and works even if
the native binary has been removed; omit `--adapter-executable` when uninstalling.

The native adapter shares the existing loss counters and atomic-write / lock
primitives, and durably spools each event as JSON under `<data>/spool/`. The
daemon resolves, builds, and admits those records into the durable JSON inbox it
already owns, alongside SQLite ownership, retention, analysis, and CLI duties.
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

Since WD-027/107 the native adapter is spool-and-forget. `agent-watchdog hook
<codex|claude>` reads JSON stdin, removes known credential forms recursively from
the complete provider input, and durably writes that input plus raw `cwd`, an
adapter-stamped `event_id`, and `received_at` under `<data>/spool/`. `cwd` is kept
only for checkout resolution. The adapter does not maintain a semantic provider
field allowlist or normalize provider telemetry.

Checkout resolution, envelope construction, per-project payload/inbox/project
quota, unknown-field warnings, and inbox admission all run in the daemon's spool
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
then admitted. A busy
project writer leaves the record for the next poll; admission/diagnostic lock
waits are at most 100 ms.

The adapter reads only `<data>/spool/limits.json`, which the daemon publishes:
the largest registered payload limit and the spool cap. When it is absent the
adapter uses built-in defaults (1 MiB payload, 64 MiB / 4096-file spool). Raw
stdin is capped at that payload limit; over it increments the `payload` counter.
A full spool increments `quota`. `event_id` / `received_at` are stamped at
arrival and trusted by the daemon, so a replayed spool record yields the same
event and the project store deduplicates it. Native handlers are synchronous
with a two-second timeout; the launch-cost measurements are not a p95 whole-hook
latency figure. WD-024 closed with recorded Windows shell-launch limitations; the
final Codex hook-performance resolution is WD-026. WD-027 removed the per-hook
config parse, redaction-regex compile, and project-tree walks, cutting the
synthetic concurrent four-caller p95 from 96 ms to 65 ms
([evidence](evidence/wd027-spool-bench.json)). Do not treat zero loss counters as
proof that a provider invoked every hook.

Normal completion and handled failures emit exactly `{}` (codex) or nothing
(claude) and exit zero. The adapter returns no continuation, denial, context, or
other control field. Missing-daemon recovery is detached and does not wait for
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
defaults or project overrides omits those fields at drain. Transcript paths and
arbitrary native fields are not copied. Known credential forms are removed in the
adapter before the spool write — the first durable write — and the daemon
re-sanitizes when it builds the canonical envelope; this is not an anonymization
or complete secret-detection claim. Quotas and persistent diagnostic counters are
described in [storage](storage.md).

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

**`timeout: 2` is fixed** — the same production budget as Codex. It is not
parameterized and is not raised to pass acceptance.

**Empty stdout is required, not stylistic.** Claude adds a hook's stdout to the
model context on `SessionStart` and `UserPromptSubmit`. Printing `{}` there would
inject model context and break the invariant that observation hooks do not
continue a turn or inject context. The adapter therefore prints **nothing** for
`claude` on every path — success, unregistered project, malformed JSON, oversized
payload, pause, and internal failure — and always exits 0. The Rust adapter makes
the same choice by scanning its raw arguments for the `claude` token, so a
parse failure is also silent. Codex still receives `{}`.

**Twelve native events map to existing envelope kinds:**

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
`error`, `duration_ms`, and `is_interrupt` fields are **not** captured in
WD-022a: widening the captured set widens the redaction surface, and it belongs
to WD-022b along with transcript enrichment.

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
