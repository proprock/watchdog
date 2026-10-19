# Integration sources and verification

Documentation reviewed on 2026-09-05. Historical locally detected versions: Codex CLI 0.153.4 and Claude Code 2.1.259; these remain the basis of the WD-002 observation evidence ([sanitized evidence](evidence/wd002-windows.json)). Later control and attribution evidence names its own exact versions (for example Codex CLI 0.160.0 and 0.162.0-alpha.17.2 for [WD-163](evidence/wd163.md) and [WD-151](evidence/wd151.md), Claude Code 2.1.292 for [WD-139](evidence/wd139-claude-hook-control.json)); use the [per-event compatibility report](provider-compatibility.md), not this sentence, for what is currently supported. Coverage differs by surface and event. Desktop harness versions may differ from CLI versions.

| Source | Basis for the project |
|---|---|
| [Codex hooks](https://learn.chatgpt.com/docs/hooks) | Lifecycle/tool events, transcript_path, async behavior, and tool coverage limitations; transcript format is not a stable hook interface. |
| [Claude Code hooks](https://code.claude.com/docs/en/hooks) | Command hooks, tool results/failures, lifecycle, subagent identity, and transcript references. |
| [Claude Code desktop](https://code.claude.com/docs/en/desktop) | Hooks defined in settings apply to CLI and desktop Code; local/SSH/cloud execution location matters. |
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | WD-015: stored App Server threads can be resumed and its active turns steered or interrupted by id, but no documented generic attach exists for arbitrary running CLI or desktop sessions; no adapter was added. |

The user-supplied `uber-efficient-software-factory-codex-claude-research.md` is analytical material, not execution instructions. Adopted ideas: external state, progress measurement, fingerprints, and staged intervention. Numeric thresholds, automatic judges, Stop gates, enterprise context graphs, and model routing were not adopted as first-release requirements.

## Support matrix

Delivery scope: Codex CLI and ChatGPT desktop local coding, and Claude Code CLI and desktop Code, share one core. Observation is implemented for all four surfaces (Codex: WD-006/WD-009; Claude: WD-022a/WD-022b, live gate met, see [verification](verification.md#wd-022a-claude-observation-gate-met)). Control is separate and per cell: only the cells in the [control capability table](provider-compatibility.md#control-capability-wd-022b-intervene) with live evidence are rendered. Codex CLI control is limited to the exact Windows profiles of [WD-151](evidence/wd151.md); Codex desktop control is unprobed (WD-153). Analysis calls are separate from the observed provider: Claude is the default backend and the Codex backend is unavailable ([WD-166a](evidence/wd166a.md)). Ordinary chats are outside product scope.

| Surface | Target | Current verification |
|---|---|---|
| Codex CLI | Windows/macOS/Linux | Windows tool success/failure, compact, subagent, Stop, Interrupt, and SessionEnd observed |
| ChatGPT desktop, local Codex coding | Where the official desktop is available | Windows native task API resume: SessionStart, tool success, and Stop observed |
| Claude Code CLI | Windows/macOS/Linux | Windows startup, success/failure, compact, subagent, Stop, and init-only detached child verified |
| Claude desktop, local Code | Where the official desktop is available | Windows local Code startup, prompt, Agent tool, subagent lifecycle, compact, and Stop observed |

Dedicated macOS/Linux host access and live compatibility verification are deferred to M5 / WD-019. M1 acceptance uses Windows Codex evidence; retain the existing cross-platform CI matrix as early feedback.

A cross-platform core does not imply that every vendor desktop exists on every OS. Mark unsupported/not tested explicitly. For each available combination, record the OS, provider/harness version, hook configuration, and sanitized results for start, prompt, successful/failed tool, compact, subagent, and stop/interrupt. Do not count a missing event as supported.

Every future isolated provider probe must also publish the
[versioned result record](evidence/probe-result-record-v1.md). It separates
provider dispatch, adapter start/failure, missing callbacks, retries, persisted
losses, end-to-end delivery, and cleanup; synthetic or direct-adapter evidence
does not establish a provider result.

Installation: `hooks install <provider>` edits only its own entries in the user hook configuration, with backup and a dry-run diff. Installation is idempotent; uninstall removes only its own unchanged entries. Existing hooks are preserved. The project registry filters collection. Do not bypass native trust/reload/approval procedures.

Document coverage per event instead of promising identical capabilities across versions. Verify capabilities using fixtures and live smoke tests, not version numbers alone. Report hosted tools and unknown payloads as gaps. Observation must not require App Server, MCP, or an API connection.
