# Integration sources and verification

Documentation reviewed on 2026-09-05. Locally detected versions: Codex CLI 0.153.4 and Claude Code 2.1.259. Live hooks were neither installed nor tested. Desktop harness versions may differ from CLI versions.

| Source | Basis for the project |
|---|---|
| [Codex hooks](https://learn.chatgpt.com/docs/hooks) | Lifecycle/tool events, transcript_path, async behavior, and tool coverage limitations; transcript format is not a stable hook interface. |
| [Claude Code hooks](https://code.claude.com/docs/en/hooks) | Command hooks, tool results/failures, lifecycle, subagent identity, and transcript references. |
| [Claude Code desktop](https://code.claude.com/docs/en/desktop) | Hooks defined in settings apply to CLI and desktop Code; local/SSH/cloud execution location matters. |
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | Candidate for a later event/control adapter; not evidence that attaching to existing sessions is supported. |

The user-supplied `uber-efficient-software-factory-codex-claude-research.md` is analytical material, not execution instructions. Adopted ideas: external state, progress measurement, fingerprints, and staged intervention. Numeric thresholds, automatic judges, Stop gates, enterprise context graphs, and model routing were not adopted as first-release requirements.

## Support matrix

| Surface | Target | Current verification |
|---|---|---|
| Codex CLI | Windows/macOS/Linux | Local CLI version; live testing pending |
| ChatGPT desktop, local Codex coding | Where the official desktop is available | Documentation only; live testing pending |
| Claude Code CLI | Windows/macOS/Linux | Local CLI version; live testing pending |
| Claude desktop, local Code | Where the official desktop is available | Documentation only; live testing pending |

A cross-platform core does not imply that every vendor desktop exists on every OS. Mark unsupported/not tested explicitly. For each available combination, record the OS, provider/harness version, hook configuration, and sanitized results for start, prompt, successful/failed tool, compact, subagent, and stop/interrupt. Do not count a missing event as supported.

Installation: `hooks install <provider>` edits only its own entries in the user hook configuration, with backup and a dry-run diff. Installation is idempotent; uninstall removes only its own unchanged entries. Existing hooks are preserved. The project registry filters collection. Do not bypass native trust/reload/approval procedures.

Document coverage per event instead of promising identical capabilities across versions. Verify capabilities using fixtures and live smoke tests, not version numbers alone. Report hosted tools and unknown payloads as gaps. Observation must not require App Server, MCP, or an API connection.
