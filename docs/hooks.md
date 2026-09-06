# Codex observation hooks

WD-006 supports the same native hook adapter for Codex CLI and local desktop
coding sessions. It neither changes harness behavior nor invokes a model. Ordinary
chats and Claude are outside this milestone.

## Register and install

Use an installed Watchdog environment with a stable Python executable location.
Generated commands use that interpreter and absolute Watchdog paths, so moving
or deleting the environment requires uninstalling and reinstalling the hooks.

Explicitly register the repository before installing observation hooks:

```console
agent-watchdog project add /absolute/project
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

The native adapter uses the existing config, locks, loss counters, and durable
JSON inbox. Python retains SQLite ownership, retention, analysis, and CLI duties.
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

`agent-watchdog hook codex` reads JSON stdin, resolves an explicitly registered
project/worktree, and atomically admits an envelope through the daemon API. Git
identity lookup has a 250 ms subprocess timeout; raw input is capped at the largest
registered payload limit before parsing, then at the selected project's limit
(1 MiB by default). Each admission/diagnostic lock waits at most 100 ms. Native
handlers are synchronous with a two-second timeout. These bounds are not a p95
whole-hook latency measurement; WD-008 measured an above-target baseline; the Rust adapter and native acceptance remain WD-024.

The WD-024 direct Rust recheck meets the adapter target, but measured Windows
shell launches remain above 250 ms and the first PowerShell 7 launch exceeded
two seconds. Concurrent native probes also have missing callbacks. See the
[separate launch measurements and open gates](verification.md#wd-024-rust-adapter-acceptance-remains-open).
Do not treat zero adapter loss counters as proof that Codex invoked every hook.

Normal completion and handled failures emit exactly `{}` and exit zero. The adapter
does not return continuation, denial, context, or other control fields. Missing
daemon recovery is detached and does not wait for readiness. Persistent pause
prevents input processing, queuing, and restart. Invalid input, unavailable Git,
registry/config errors, and storage/launch failures are ignored by the hook.
Missing Python or a broken installation cannot be handled inside Python; repair
the installation if the product reports command-launch errors.

Metadata includes session/turn/agent identifiers, checkout identity, event kind,
tool-call ID/name, and tool-response type. Identifiers are bounded. WD-007 also
captures the native `prompt`, `tool_input`, `tool_response`, and
`last_assistant_message` fields under `payload.codex.content`, when present.
Capture defaults to true for registered projects; `capture_content = false` in
defaults or project overrides omits those fields. Transcript paths and arbitrary
native fields are not copied. Known credential forms are removed before any
persistent write; this is not an anonymization or complete secret-detection claim.
Quotas and persistent diagnostic counters are described in [storage](storage.md).

CLI versus desktop and backend version remain unknown unless evidence supplies
them; the adapter does not guess from cwd or the installed CLI version. Tool-call
IDs are not event IDs. A PostToolUse string is not parsed as a universal exit code,
and Stop means turn end, not task success. Missing native fields remain null.

## Verification

Offline pytest covers known synthetic Codex payload shapes, opaque and structured
tool responses, exact no-op output on failures, input bounds, registry exclusion,
persistent pause, shell quoting, idempotent installation, original-file recovery,
later user edits, edited owned hooks, and interrupted installation. It never
installs into an active provider configuration or invokes a model.

Live Windows evidence and remaining limits are recorded in
[verification](verification.md). macOS/Linux live checks remain WD-019.
