# WD-002: provider compatibility spike

Date: 2026-09-05. Windows, CPython 3.12.13. The locally resolved Codex version command reported 0.153.4; the interactive probe banner reported 0.153.3. Claude CLI reported 2.1.259. Desktop backend versions are not inferred from installed CLI versions.

**Status:** WD-002 is complete as a compatibility investigation with user-accepted limitations. Subsequent Codex lifecycle evidence is in the [WD-005 daemon report](daemon.md); remaining Codex desktop event checks moved from WD-008 to WD-024 after the Python adapter timed out in native probes. Claude observation is implemented in WD-022a (adapter, installer, offline tests, a live Windows Claude CLI Pass A re-run, and a supervised desktop pass; the gate is met — see [verification](verification.md#wd-022a-claude-observation-gate-met)); enrichment and guidance/control remain WD-022b. The Claude cells below marked "WD-022a" come from those live passes through the production adapter; unmarked Claude cells are WD-002 capture-probe history.

This historical report distinguishes live observations from documented capabilities and untested combinations. The subsequent [WD-006 adapter and installer](hooks.md) now provide metadata-only observation; current live results are in [verification](verification.md).

## Evidence

[Sanitized live records](evidence/wd002-windows.json) contain allowlisted field types and stable aliases replacing hashed native IDs. They contain no prompts, commands, paths, transcripts, or tool output text. Counts include diagnostic attempts and resumed sessions; they are not task or unique-action counts. A direct synthetic Codex no-op check is excluded. The existing examples under tests/fixtures/hooks remain synthetic, not recordings.

The opt-in `scripts/capture_hook.py` reads at most 1 MiB, writes a temporary file and renames it, and fails open on malformed input or storage errors. It uses empty stdout for Claude and `{}` with `--json-noop` for Codex. This is a diagnostic probe, not a production adapter: it has no registry, retention, loss accounting, or daemon startup.

## Windows event coverage

| Event / operation | Codex CLI | Codex desktop task API | Claude CLI | Claude desktop Code |
|---|---|---|---|---|
| SessionStart | Not captured on initial launch before trust | Captured on resume | WD-022a: stored via the adapter; after removing the git subprocess from resolution, four concurrent `claude -p` starts each stored a complete set (no drop, zero losses) | WD-022a: stored on folder selection |
| UserPromptSubmit | Captured | Not observed in API-submitted turn | WD-022a: stored (`turn.start`), empty stdout accepted | Captured |
| PreToolUse / PostToolUse | Captured | Captured for one successful shell call | WD-022a: stored for every Bash call (8/8 `tool_use_id` correlated to `tool.finish`) | Captured for Agent |
| Nonzero shell exit | PostToolUse captured after exit 1 | Not exercised | WD-022a: `PostToolUseFailure` stored as `tool.finish`, `tool_outcome` unknown | WD-022a: `PostToolUseFailure` stored as `tool.finish` |
| PreCompact / PostCompact | Captured via /compact | Not exercised separately | WD-002 via /compact; not re-exercised in the WD-022a CLI pass | WD-022a: stored via /compact (`compaction.start`/`compaction.end`) |
| SubagentStart / SubagentStop | Captured for one child | Not exercised separately | WD-002 for one child; not re-exercised in the WD-022a CLI pass | WD-022a: `SubagentStart` → `agent.start` and `SubagentStop` → `agent.end` stored for a Task-tool subagent |
| Stop | Captured with no-op JSON | Captured | WD-022a: stored (`turn.end`) with empty stdout | Captured with empty stdout |
| Interrupt | Captured after Escape during active turn | Not exercised separately | No such native event exists in Claude Code | WD-022a: Escape during an active tool call produced no event (none exists); the call still completed and emitted `PostToolUse` |
| SessionEnd | Captured on /quit | Not exercised separately | WD-022a: stored (`session.end`) for all four Pass A sessions | WD-022a: stored (`session.end`) when the session is archived (the Code tab has no "close") |
| Notification | n/a | n/a | WD-022a: installed and mapped to `waiting`; not exercised | WD-022a: installed and mapped to `waiting`; not exercised |

Codex desktop evidence comes from resuming the same idle scratch session through the native desktop task API. This proves hooks on that execution path, not every GUI operation or backend. Attempting concurrent ownership while the TUI was open failed with an active-writer error; after /quit the API turn completed. This is probe orchestration, not an App Server dependency for Watchdog.

Claude desktop uses project-local `.claude/settings.local.json` in a dedicated scratch folder. A bounded no-shell prompt launched one no-tool subagent and completed with DESKTOP_PROBE_DONE. Startup, prompt, Agent tool, subagent lifecycle, Stop, and manual PreCompact/PostCompact summaries were received. The app showed the Sonnet 5 model label; no desktop backend version was established. The folder picker required one manual user action because UI automation reported stale focus; this is a probe tooling issue, not a hook limitation.

## Payload and installation findings

- Codex tool samples included session_id, turn_id, tool_use_id, model, permission_mode, tool_input, and tool_response. The observed tool_response was a string, so adapters must not assume a universal JSON result or exit-code property.
- Claude init-only SessionStart supplied session_id, transcript_path, cwd, hook_event_name, and source. Optional metadata was absent. Successful Bash results contained stdout, stderr, and interrupted; no exit_code was present in that sample. Failure events supplied error, is_interrupt, and duration_ms.
- Both providers supplied agent_id on subagent lifecycle events. Preserve missing IDs; do not invent a turn ID or infer desktop/CLI from cwd. Evidence aliases are local to each provider dataset, not globally unique identities.
- Stop records turn completion, not task success. A user interrupt during an active Codex turn was observed; cancellation of an already running shell process was not established.
- Codex required native review of the exact 11-hook configuration. The user explicitly approved trust and the TUI applied it. No trust-bypass flag or fabricated trust record was used. A changed configuration required review again.
- Initial Codex hooks failed due to Windows quoting and diagnostic timeouts. Adding `commandWindows` with PowerShell's `&` call operator fixed the quoted executable invocation. Most diagnostic timeouts were raised to 10 seconds; Interrupt/SessionEnd used 3 seconds. This does not validate the production latency target of 250 ms or the planned 2-second timeout.
- An empty JSON object was exercised as Codex's no-op response, including Stop/SubagentStop. Claude accepted empty stdout. Never return block/continue/context fields in observation mode.

## WD-107 telemetry inventory and gaps

The daemon recognizes current hook identifiers and lifecycle/tool fields, plus
model and alias, reasoning mode/effort (`reasoning_effort` and the `effort`
alias), provider/client version, surface, context/cache/token counters, timing,
retries, interruptions, permission mode and outcome, errors, and parent
relations. Transport identity also covers `prompt_id` and `scratchpad_dir`, and
Claude session state covers `background_tasks`, `session_crons`, and
`session_title`. Values are retained only when a provider actually supplies them;
absence stays `unknown` or `unavailable`, never zero. The current fixtures
establish only the fields listed above in the payload findings; they do not
establish that either provider exposes every telemetry signal on every surface.

An input field outside this inventory remains in provider metadata and produces a
WARNING with its safe top-level name, so it can be assessed and added deliberately.
No model-quality, task-progress, or control inference follows from that retention.
WD-012 must calibrate any later heuristic on 20-50 manually labelled real sessions,
including sessions with no finding, and report precision plus false positives and
false negatives. This task authorizes no live provider probe.

## Process lifetime

`uv run python scripts/detach_probe.py` verifies that a detached Python child with disconnected standard handles responds after its intermediate parent exits, then signals completion within a bounded lifetime.

`uv run python scripts/detach_probe.py --claude-init` uses temporary explicit settings, disabled ordinary settings sources, and no configured MCP servers. SessionStart launches the same child; release occurs only after Claude exits. The restricted execution environment returned exit 0 without a marker, which was inconclusive. The same experiment outside that environment passed. The cause of the restricted-run difference was not established.

| Harness exit experiment | Result |
|---|---|
| Generic Python parent, Windows | Verified child response after parent exit |
| Claude init-only SessionStart, Windows | Verified child response after Claude exit |
| Codex CLI and both desktop process containers | Not tested; no survival claim |

The subsequent [WD-005 report](daemon.md) verifies singleton ownership, crash recovery, actual CLI exit, and desktop task unload. The user accepted task unload as the desktop lifecycle boundary for WD-005; full application shutdown and logout persistence are not claimed. Claude CLI/desktop survival is required under WD-022; the generic experiment is not a substitute. macOS/Linux host access and compatibility verification are deferred to M5 / WD-019 by the user's decision.

## Reproduction and verification

Run offline checks with `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`. The capture tests were first run red with the script absent, then passed after implementation. Current offline result: 8 tests passed, Ruff lint and formatting passed. Live provider calls never run in pytest/CI.

For live checks, configure the capture script only in a dedicated scratch project, approve native trust through the product, then explicitly exercise harmless success/failure, one no-tool child, manual compaction, completion, and interruption. For Claude CLI, put `--` before the `/compact` positional prompt when using variadic `--tools`; otherwise the prompt can be consumed as a tool argument. Two malformed diagnostic invocations failed before the corrected compaction call succeeded.

Successful Claude CLI model probes reported costs of $0.042125 (tool success/failure), $0.0289305 (subagent), and $0.12663375 (compaction). These are observed run costs, not pricing guarantees. Init-only did not start a model conversation. Provider-owned histories may retain the synthetic conversations; Watchdog does not delete vendor transcripts.

Cleanup: all three scratch hook settings files were renamed with a `.disabled` suffix. No global hook configuration was changed. The owned Codex TUI exited; the Codex and Claude desktop probe tasks were archived. Capture hooks do not leave child processes running. Native trust was not rewritten or bypassed during cleanup.

## Sources

[Codex hooks](https://learn.chatgpt.com/docs/hooks), [Claude hooks](https://code.claude.com/docs/en/hooks), and [Claude desktop](https://code.claude.com/docs/en/desktop). Documentation is a capability reference, not live proof. Codex background hooks can be cancelled at session end, so durable collection must remain synchronous and bounded; hook lifetime and detached-core lifetime are separate concerns.
