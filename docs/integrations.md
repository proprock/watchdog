# Integration sources and verification

Documentation reviewed on 2026-09-05. Locally detected versions: Codex CLI 0.153.4 and Claude Code 2.1.259. Windows live probes exercised both CLIs, the Codex desktop task API, and Claude desktop Code; see [the per-event compatibility report](provider-compatibility.md) and [sanitized evidence](evidence/wd002-windows.json). Coverage differs by surface and event. Desktop harness versions may differ from CLI versions.

| Source | Basis for the project |
|---|---|
| [Codex hooks](https://learn.chatgpt.com/docs/hooks) | Lifecycle/tool events, transcript_path, async behavior, and tool coverage limitations; transcript format is not a stable hook interface. |
| [Claude Code hooks](https://code.claude.com/docs/en/hooks) | Command hooks, tool results/failures, lifecycle, subagent identity, and transcript references. |
| [Claude Code desktop](https://code.claude.com/docs/en/desktop) | Hooks defined in settings apply to CLI and desktop Code; local/SSH/cloud execution location matters. |
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | Candidate for a later event/control adapter; not evidence that attaching to existing sessions is supported. |

The user-supplied `uber-efficient-software-factory-codex-claude-research.md` is analytical material, not execution instructions. Adopted ideas: external state, progress measurement, fingerprints, and staged intervention. Numeric thresholds, automatic judges, Stop gates, enterprise context graphs, and model routing were not adopted as first-release requirements.

## Support matrix

Delivery scope: M1-M4 cover Codex CLI and local desktop coding only. Claude observation (adapter + installer) is implemented in WD-022a; a live Windows Claude CLI pass ran but did not meet its gate (see [verification](verification.md#wd-022a-claude-observation-gate-not-met)). Claude enrichment, lifecycle beyond observation, and guidance/control are WD-022b, after WD-014. Ordinary chats are outside product scope.

| Surface | Target | Current verification |
|---|---|---|
| Codex CLI | Windows/macOS/Linux | Windows tool success/failure, compact, subagent, Stop, Interrupt, and SessionEnd observed |
| ChatGPT desktop, local Codex coding | Where the official desktop is available | Windows native task API resume: SessionStart, tool success, and Stop observed |
| Claude Code CLI | Windows/macOS/Linux | Windows startup, success/failure, compact, subagent, Stop, and init-only detached child verified |
| Claude desktop, local Code | Where the official desktop is available | Windows local Code startup, prompt, Agent tool, subagent lifecycle, compact, and Stop observed |

Dedicated macOS/Linux host access and live compatibility verification are deferred to M5 / WD-019. M1 acceptance uses Windows Codex evidence; retain the existing cross-platform CI matrix as early feedback.

A cross-platform core does not imply that every vendor desktop exists on every OS. Mark unsupported/not tested explicitly. For each available combination, record the OS, provider/harness version, hook configuration, and sanitized results for start, prompt, successful/failed tool, compact, subagent, and stop/interrupt. Do not count a missing event as supported.

Installation: `hooks install <provider>` edits only its own entries in the user hook configuration, with backup and a dry-run diff. Installation is idempotent; uninstall removes only its own unchanged entries. Existing hooks are preserved. The project registry filters collection. Do not bypass native trust/reload/approval procedures.

Document coverage per event instead of promising identical capabilities across versions. Verify capabilities using fixtures and live smoke tests, not version numbers alone. Report hosted tools and unknown payloads as gaps. Observation must not require App Server, MCP, or an API connection.
