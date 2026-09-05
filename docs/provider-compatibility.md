# WD-002: provider compatibility spike

Date: 2026-09-05. Versions inspected locally: Codex CLI 0.153.4; Claude Code 2.1.259; Windows with CPython 3.12.13. This is an incremental spike report, not a declaration of full provider support.

## Verified behavior

`uv run python scripts/detach_probe.py` starts an intermediate Python parent, which starts a detached child with disconnected standard handles. The controller waits for the parent to exit, then releases the child and requires a response. The child signals completion and has a bounded lifetime. This checks survival after parent exit, not logout persistence, singleton locking, or a production daemon.

`uv run python scripts/detach_probe.py --claude-init` runs Claude with temporary explicit settings, no user/project/local settings sources, and no configured MCP servers. `--init-only` executes the SessionStart hook without starting a model conversation. Managed settings remain applicable. The hook launches the same child; the release is sent only after Claude exits. Temporary configuration is removed; user hook configuration is untouched.

Results:

| Experiment | Result |
|---|---|
| Offline Python parent, Windows | PASS: parent exited, stdout empty, child responded after release and signaled completion |
| Claude init-only inside restricted execution sandbox | INCONCLUSIVE: exit 0, no child marker; this did not prove hook execution |
| Same Claude experiment outside execution sandbox | PASS: SessionStart received, Claude exited, stdout empty, detached child responded and signaled completion |

Only the execution environment changed in the successful retry. The exact cause of the restricted-run failure was not established; do not classify all sandboxed Claude hooks as unsupported.

Sanitized live SessionStart shape: string fields `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `source`. No model, turn ID, or permission_mode appeared in this sample. The probe stores only field names/types and the event name; session identifiers, paths, and transcript contents are not retained. No paid model request, agent task, or subagent was launched.

## Coverage and implementation implications

| Concern | Codex | Claude Code |
|---|---|---|
| Hook configuration | Native review of the exact hook hash is required; CLI `/hooks` manages trust | Explicit temporary settings exercised by init-only |
| Tool failure | Documented PostToolUse also covers nonzero shell exit | Documented PostToolUseFailure has error/is_interrupt/duration fields; pre-execution rejection is a separate case |
| Subagent identity | Parent session ID plus agent_id on lifecycle events | agent_id on lifecycle events; child transcript reference on stop |
| Raw tool result | Tool-specific JSON; no universal exit-code property | Tool-specific result or separate failure fields |
| Missing identifiers | Preserve unknown; do not infer desktop/CLI from cwd | Live startup sample demonstrates missing optional metadata |
| Stop | Turn completion is not task success; continuation feedback must remain disabled | Same observation requirement |

Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Claude hooks](https://code.claude.com/docs/en/hooks). Codex background hooks can be cancelled at session end. Keep initial durable collection synchronous and bounded; detached-core survival must be checked separately from async hook lifetime. Codex Stop/SubagentStop output handling also needs a live no-op check before promising empty stdout for every event.

## Remaining live matrix

| Surface / OS | Startup hook | Post-tool / failure / compact / subagent / stop / interrupt | Detached child after actual harness exit |
|---|---|---|---|
| Claude CLI / Windows | VERIFIED via init-only | NOT TESTED | VERIFIED for init-only SessionStart path |
| Codex CLI / Windows | NOT TESTED: native hook trust review needed | NOT TESTED | NOT TESTED with Codex |
| ChatGPT desktop local coding / Windows | NOT TESTED | NOT TESTED | NOT TESTED |
| Claude desktop local Code / Windows | NOT TESTED | NOT TESTED | NOT TESTED |
| CLI and available desktops / macOS, Linux | NOT TESTED: no host available in this run | NOT TESTED | NOT TESTED |

Do not use the generic Windows probe as evidence for Codex or desktop process containers. The next live checks need a trusted temporary hook definition in Codex and interactive sessions on each available surface. No trust-bypass flag or fabricated trust record was used. WD-002 remains open until those checks are completed or an explicit support limitation is accepted.

## Reproduction and limits

Offline: `uv run pytest tests/test_detach_probe.py -q`. Live Claude startup: `uv run python scripts/detach_probe.py --claude-init`. The live probe is opt-in and never runs in pytest/CI. It assumes Claude's native hook shell is available and rejects shell-sensitive path characters. It is an experiment, not a deployable collector or installer.

For remaining events, use a dedicated trusted scratch project and reviewed hook configuration. Run a harmless successful command and a failing command, then explicitly exercise compaction, subagent lifecycle, stop, and interrupt. Record per-event field presence and native IDs after sanitization. Preserve missing events as not tested or unsupported with a reason. Remove only the probe configuration and owned processes afterward. Do not automatically authorize tool or hook trust requests.
