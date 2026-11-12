# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Context efficiency audit with reconciled provider usage, real Claude user turns,
  selected-session analysis, cache/cost rankings, overlap and error-volume metrics.
- Local aggregate-prompt price previews and a scenario calculator with cache,
  compaction overhead, reacquisition, and break-even sensitivity assumptions.
- Opt-in native/70%/85% Codex and Claude launch profiles, support detection and
  local experiment records; no global configuration or source-log modification.
- Reproducible local corpus audit and evidence-qualified recommendations.


- A dedicated `/context-analysis` dashboard for explicit, local reconstruction
  of the observable context at every model call. It includes longest-session
  rankings, provider cohorts, compaction recovery, repeated-content signals,
  and a stacked visible-bytes timeline alongside exact token counters.
- Privacy-preserving `context_events` captured during the normal incremental
  import. The table stores sizes, categories, anonymous SHA-256 fingerprints,
  model-call usage and compaction coordinates, but not raw prompts, tool output,
  paths, commands, ciphertext size, or decrypted reasoning.
- Deterministic compaction advice with evidence thresholds and a ready-to-use
  `compact_prompt` draft. Optional Claude/Codex advice sends aggregate metrics
  only and reuses the provider/model/prompt-version cache.
- Codex `compacted.replacement_history` support with paired
  `context_compacted` marker deduplication; Claude pre/post token and trigger
  metadata is used when available.

- Independent Claude and Codex dashboard Coach cards with configurable model
  menus, separate run/refresh controls and provider/model-specific cached advice.
  Changed metrics or prices mark saved advice stale. Codex uses ephemeral CLI
  sessions and structured output; switching models never starts an LLM request.
- Optional per-model `cache_write_5m`, `cache_write_1h`, `batch_input`, and
  `batch_output` pricing fields. Current estimates prefer `cache_write_5m` over
  legacy `cache_write`; one-hour and Batch rates are reserved for future usage
  accounting. Default pricing includes Opus 5, Opus 4.6, Sonnet 5, and extended
  Haiku 4.5 rates.
- Codex ingestion parity pass:
  - exit code and wall time parsed from the `Exit code:/Wall time:` header of
    `function_call_output` (Codex CLI >= ~0.129 no longer emits
    `exec_command_end`, so failure metrics for recent sessions were empty);
  - `custom_tool_call_output` metadata (`exit_code`, `duration_seconds`) for
    `apply_patch` telemetry;
  - `mcp_tool_call_end`: MCP calls get category `mcp`, server/tool in extra,
    duration and Ok/Err error status;
  - `turn_aborted` (user interruptions), `thread_rolled_back`, `task_started`
    rows and `codex_error_info` on error rows;
  - `session_meta.source` becomes the Codex `entrypoint` (`vscode`,
    `subagent:<kind>`); `rate_limits.plan_type` stored per session.
- Codex subagent threads (`thread_source = "subagent"`): child rollouts are
  linked to their parent via `parent_session_id`, mirrored into
  `subagent_runs` (agent type e.g. `guardian`), marked `is_noise` to avoid
  double counting, and listed on the parent session page with links.
- Per-session turn stats for Codex: `turn_count`, `turn_abort_count`,
  summed `task_duration_ms`, averaged `time_to_first_token_ms` — recomputed
  idempotently from message rows.
- `peak_context_tokens` for both agents (max per-turn context) and
  `web_search_requests` counted for Codex web tool calls.
- Agent-availability flags on every metrics report: agent-specific metrics
  are shown only when the filtered data contains them and are labeled
  "(claude only)" / "(codex only)" across `wdi stats`, the static report and
  the web dashboard; Claude-only cache-miss/hook analysis is skipped for
  Codex-only filters.
- New overview metrics: turns and interrupted turns, rate-limit peak usage
  (codex only); waste adds interrupted turns.
- Web filters: period preset dropdown (last day/week/month/year/all time,
  custom bookmarked values preserved) and a project dropdown fed from known
  projects.
- Codex session detail: effort/approval/sandbox, turns, task time, avg TTFT,
  peak context vs window, rate limit and plan, parent-session link, and the
  context-growth chart (built from `token_count` events).
- Activity chart splits sessions per agent when both agents are present.

### Changed

- Schema change requires a DB rebuild so existing sessions receive structural
  context events: delete `~/.wdi/wdi.sqlite` and re-run `wdi import`.

- `max_context_pressure` now uses `peak_context_tokens / context_window`
  instead of whole-session token sums; values above 100% indicate a turn that
  spanned a forced compaction.
- `sessions.task_duration_ms` is now the sum over turns (was: last turn);
  `sessions.time_to_first_token_ms` is now the average (was: last turn).
- Tool result merging is first-writer-wins for `exit_code`, `duration_ms` and
  `output_bytes` so precise `exec_command_end` telemetry is never overwritten
  by the coarser output header.
- `truncated_outputs` and hook summaries are explicitly gated to Claude
  sessions.
- `wdi agents` accepts `--agent` and includes Codex subagent runs (previously
  hard-coded to Claude).
- Schema change requires a DB rebuild: delete `~/.wdi/wdi.sqlite` and re-run
  `wdi import`.

- Claude ingestion now extracts more efficiency signals from transcripts:
  `stop_reason` per message (detect `max_tokens` truncations), server-side
  web search/fetch request counts, session `entrypoint`, compaction metadata
  (`pre_tokens`/`post_tokens`/`trigger`/`duration_ms`) and API-error retry
  details in system message `extra`.
- Claude tool calls now get `output_bytes` (tool_result payload size) and
  `duration_ms` derived from the call→result timestamp delta (includes
  approval wait, so it is latency rather than pure execution time).
- Sessions carry an `is_noise` flag: headless `sdk-cli` one-shots (e.g.
  background observers) are marked at import so metrics can exclude them.
- `coach_reports` table for cached dashboard-level coaching advice (consumed
  by the upcoming Coach feature).
- Schema change requires a DB rebuild: delete `~/.wdi/wdi.sqlite` and re-run
  `wdi import`.
- Noise filtering: background sessions are hidden from all metrics by default;
  re-include them with `--include-noise` (CLI) or the "Include background
  sessions" checkbox (web filters).
- New metrics: waste signals (repeated reads, duplicate commands, failed-call
  output volume, `max_tokens` truncations), cache economics trend, per-tool
  latency percentiles (p50/p95) and output volume, hook usage summary,
  subagent ROI (total tokens, tokens per edit) and "subagent opportunities"
  (exploration-heavy sessions that ran no subagents).
- Cache-miss attribution: misses are classified as cold start, tool wait
  (cache TTL expired while tools ran), user idle, or other, with wasted-token
  and cost estimates; shown in `wdi stats`, the static report, and the
  dashboard doughnut chart.
- Web UI: per-session context-growth chart with compaction sawtooth, waste
  panel and re-read file list on the session page; new `/tools` page with
  per-tool drill-down into sessions; dashboard cards/charts for waste, cache
  trend and cache misses.
- `wdi stats` prints tool detail (error rate, p50/p95), shell failure
  clusters, waste and hook tables; `wdi agents` adds total tokens and a
  subagent-opportunity hint.
- Coach: a dashboard button sends the aggregate metrics to the model
  configured in `features.llm_analysis` (per-analyzer override key: `coach`)
  and renders improvement advice, including suggested CLAUDE.md rules.
  Results are cached per filter in the new `coach_reports` table; "Refresh
  advice" forces a re-run, and backend failures render a retry state instead
  of a 500.

- Serena project configuration for Python-only LSP symbol tools, with agent
  guidance for choosing between Serena and codebase-memory-mcp.
- `wdi import` and `wdi init` now show Rich progress bars while importing
  session files, matching the long-running feedback style of `wdi analyze`.
- `wdi analyze` now shows Rich progress while building the pre-flight estimate
  before printing the cost confirmation table.
- Session ingestion now captures compact derived signals for workflow analytics:
  Codex rate-limit/TTFT/context-pressure inputs, command and patch summaries,
  Claude hook/cache-TTL usage details, and parent linkage for subagent runs.
- Static reports now surface context pressure, TTFT, tokens per edit, repeated
  reads, and shell failure clusters by command kind.
- LLM analysis adds a `workflow-recommendations` analyzer and Codex source
  transcript segmenting so prompt and summary contexts can use full local
  rollout data instead of DB previews when the source file is available.

- `wdi analyze --force/-f` — re-run analysis on already-cached sessions,
  overwriting stored results (e.g. to pick up the fuller transcript context).
- Web UI: real session names (Claude/Codex title, then first-prompt snippet, then
  short id) in the sessions list, detail header, and CLI output.
- Web UI: unicode quality badges with an on-page legend flagging notable sessions
  and subagents — ⭐/⚠️ from the LLM summary score, 🐢 slow, 🔥 error-prone,
  🔁 oversearching.
- Web UI: drill-down from a session (or a subagent) into its tool calls and
  messages, with on-demand full tool input/output read from the original
  transcript (`/sessions/{id}/stream/{stream_id}`), falling back to stored
  previews when the source file is absent.
- LLM analysis now reads the original Claude transcript at full fidelity and
  sieves it per analyzer (prompt-quality: full user prompts; summary: prompts,
  assistant text, error outputs) instead of truncated previews; Codex and
  source-less sessions fall back to previews.
- Project scaffolding: uv, src-layout, ruff, pyright, pytest.
- YAML configuration (`~/.wdi/config.yaml`): sources, features, pricing, sync.
- `wdi config init` command — creates the config and generates a `machine_id`.
- `wdi init` command — one-step setup: creates the config if missing (`--force`
  to recreate) and imports all sources.
- SQLite layer: connection factory (foreign_keys, WAL), migrations keyed on
  `user_version`, normalized schema (machines, projects, sessions, messages,
  tool_calls, subagent_runs, import_state, llm_analyses), repository layer.
- `wdi import` command: incremental import of Claude Code sessions (main
  transcripts plus step-by-step subagent traces as separate stream_id),
  project grouping with aliases, byte-offset resume of append-only files.
- OpenAI Codex session import: rollout files, call/result correlation by
  call_id (exec telemetry with exit_code/duration, patches), tokens from
  token_count (including reasoning), TTFT and turn durations, compactions, errors.
- Metrics layer: session filters (agent, project, ISO/relative `since`), cost
  estimation from a per-model pricing table (longest-prefix match), and an
  assembled `MetricsReport` reused across surfaces.
- `wdi stats` — overview, tokens/cost by model, tool usage and failures, projects.
- `wdi sessions` / `wdi sessions show <id>` — recent sessions and per-session detail.
- `wdi agents` — subagent efficiency by type from Claude subagent traces
  (avg tokens/calls, search-to-edit ratio, redundant re-reads, trace error rate).
- `wdi report` — self-contained static HTML report (vendored Chart.js 4.4.6,
  CSS and data inlined, works offline): overview cards, activity/token/tool/model
  charts, and tables for models, tools, failures, subagents, and projects.
- `wdi serve` — local FastAPI web server (read-only) for browsing analytics live:
  dashboard with HTMX filters (agent/project/since, bookmarkable), a sessions list
  and per-session detail. The detail page shows stored LLM analysis (prompt
  quality, session summary, agent efficiency) with score, summary and advice when
  the session has been analyzed. `--host`/`--port` override the `server` config
  block; `--import` runs an incremental import before serving. Vendored htmx and
  Chart.js, no frontend build step.
- LLM analysis via the local `claude -p` backend (no API key): analyzers
  `prompt-quality`, `summary`, `agent-efficiency`; results cached in
  `llm_analyses` and keyed by prompt version. `wdi analyze` with per-analyzer
  model selection (`features.llm_analysis.models`, `--model` override).
- `wdi analyze` pre-flight estimate: before any LLM call it prints the sessions,
  approximate input/output tokens (~4 chars/token) and approximate cost per
  analyzer, then asks for confirmation. `--yes/-y` skips the prompt; cached
  sessions are excluded and short model aliases are mapped to the pricing table.
- Snapshot sync for cumulative cross-machine statistics: `wdi export-snapshot`
  writes `wdi-<machine_id>.sqlite`; `wdi import-snapshot` merges other machines'
  snapshots (idempotent INSERT OR IGNORE, projects merged by canonical_name).
- `wdi mcp` — Model Context Protocol server (stdio, read-only) exposing four
  tools that reuse the metrics query layer: `get_recent_sessions`,
  `get_tool_failure_patterns`, `get_oversearching_examples`, and
  `get_prompt_quality_report` (reads pre-computed `prompt_quality` analyses;
  run `wdi analyze prompt-quality` first). All tools accept the standard
  `agent`/`project`/`since`/`limit` filters.

### Fixed

- LLM analysis now excludes Claude observer tool-event payloads from developer
  prompt context, reducing duplicated raw tool I/O in cost estimates and prompts.
