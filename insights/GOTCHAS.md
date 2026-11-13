# Gotchas

Non-obvious findings, fixes, and traps collected while building wdi. Read this
before touching ingest, the LLM backend, packaging, or the SQLite merge.

## Build & packaging

- **hatchling backend id**: `build-backend = "hatchling.build"`, not
  `"hatchling"`. The wrong value fails with
  `module 'hatchling' has no attribute 'build_editable'`.
- **Wheel data files**: hatchling ships non-`.py` files under the package by
  default. `0001_init.sql`, `report/templates/*.html`, and
  `report/static/*` (CSS/JS/Chart.js) end up in the wheel without extra config.
  Verify with `uv build --wheel` and inspect the archive if you add new asset
  dirs.
- **Chart.js is vendored** at `src/wdi/report/static/chart.umd.min.js`
  (v4.4.6, ~205 KB). The report inlines it so it works offline. Do not switch to
  a CDN. If you bump the version, the test asserting `"Chart.js v4.4.6"` in the
  HTML must be updated.

## Config / pydantic

- **Field defaults skip validators.** A `field_validator` does NOT run on a
  model default unless the field uses `Field(default=..., validate_default=True)`.
  `Settings.db_path` needed this so the default `~/.wdi/wdi.sqlite` gets
  expanded; otherwise a round-trip (create -> load) produced unequal objects.
- **Per-analyzer model override** lives in `features.llm_analysis.models`
  (`{analysis_type: model}`) with fallback to `features.llm_analysis.model`; the
  `--model` CLI flag overrides both. See `LlmAnalysisConfig.model_for`.

## Windows specifics

- **Legacy console encodings (cp1250 etc.) cannot encode some characters** and
  crash Rich/Typer output with `UnicodeEncodeError`. `cli.main()` calls
  `_ensure_unicode_output()` to `reconfigure(errors="replace")` non-UTF-8
  stdout/stderr. Keep this guard even though UI strings are English now (em-dash,
  `≈`, etc. still appear).
- **Command-line length limit → `WinError 206` surfaces as `FileNotFoundError`.**
  Passing a long prompt as an argv to `claude -p` raised
  "filename or extension too long", which Python reports as FileNotFoundError —
  it looked like "claude CLI not found". Fix: feed the prompt on **stdin**
  (`subprocess.run(..., input=prompt)`), not as an argument. Analyzer context is
  also capped (`Analyzer.max_context_chars`).
- **PowerShell pipe truncation** (`... | Select-Object -First N`) closes the pipe
  early and makes a process report exit 255 / BrokenPipe even when it succeeded.
  Redirect to a file (`*> out.txt`) to read the real exit code.
- LF→CRLF git warnings on Windows are benign.

## `claude -p` backend

- Invocation: `claude -p --output-format json --model <model>`, prompt on stdin.
  No API key needed; auth comes from the installed CLI.
- Output JSON: text is in `.result`; failure is `.is_error` (+ `.subtype`).
- With no stdin it warns `no stdin data received in 3s, proceeding without it`.
- Responses may be wrapped in a ```json fence; `_parse_response` strips it and
  falls back to `{"raw": text}` when the body is not JSON.
- **Model alias vs pricing key mismatch.** `wdi analyze` passes the CLI's short
  aliases (`haiku`/`sonnet`/`opus`), but `PricingResolver` matches by *pricing
  key as a prefix of the model id* — `"haiku"` never matches `"claude-haiku-4-5"`,
  so a naive cost lookup returns `None`. The pre-flight estimate maps aliases via
  `_MODEL_ALIASES` in `llm/service.py` before pricing. The cost estimate also
  assumes a fixed `_EST_OUTPUT_TOKENS_PER_CALL` (output length is unknown
  up front) and `~4 chars/token` for input.

## Dashboard coaching

- **Coach cache identity** includes provider and model as well as filters; the
  existing `filter_hash` column stores the composite key. The JSON result wraps
  advice with a metrics fingerprint to detect stale data/prices without a schema
  change. Older unscoped cache rows are not reused.
- **Codex coaching** reads the final answer from `--output-last-message`, not
  stdout, which can contain execution events. Use stdin for the prompt,
  `--output-schema` for the response shape and `--ephemeral` to avoid importing
  the analysis itself as another session.
- **Windows CLI resolution** can differ between PowerShell and Python launched
  by `uv`: `shutil.which("codex")` may find an older npm `codex.CMD` while the
  shell finds the newer app-bundled executable. Use
  `features.llm_analysis.codex_executable` to select the intended installation;
  otherwise new models can fail with a CLI upgrade requirement.

## Session formats (see `docs/`)

### Claude Code
- Main transcript `<sessionId>.jsonl` sits **beside** the `<sessionId>/`
  directory, not inside it. Subagent traces are under
  `<sessionId>/subagents/agent-*.jsonl`, every line `isSidechain: true`.
- A subagent run's aggregate telemetry comes from the parent `user` record whose
  `toolUseResult` carries `agentId` (+ `agentType`, `toolStats`, totals).
- tool_use ↔ tool_result correlate by `id` / `tool_use_id`.
- Tokens are on `assistant.message.usage` per response (sum across the session).
- `entrypoint` values observed in real data: `cli`, `claude-desktop`,
  `sdk-cli`. Background observer one-shots are `sdk-cli` — that is the
  `is_noise` rule. Do **not** flag "everything except `cli`": desktop
  sessions are interactive.
- `compactMetadata` keys: `trigger`, `preTokens`, `postTokens`, `durationMs`
  (older CLI versions may omit `postTokens`). `api_error` system records
  carry `apiErrorStatus`, `retryAttempt`, `maxRetries`, `retryInMs`.
- Claude reports no tool execution time; `tool_calls.duration_ms` is derived
  from the tool_use → tool_result timestamp delta and includes permission
  prompt wait, so UI copy calls it "latency".

### Codex
- Lines are envelopes `{type, timestamp, payload}`; semantics in `payload.type`.
- **Tokens are not on messages** — they arrive as separate `token_count` events.
  `info.total_token_usage` is cumulative; `info.last_token_usage` is per-turn.
  `reasoning_output_tokens` exists only here.
- A call's result appears twice: `exec_command_end` (rich: exit_code, duration,
  aggregated_output) usually **before** `function_call_output`. Hence
  `apply_tool_result` keeps the first non-null `output_preview`
  (`COALESCE(output_preview, ?)`), and `ToolResultRec.is_error` is **tri-state**
  (`None` = leave unchanged) so a later event can't clobber the flag.
- **Format drift: `exec_command_end` disappeared in CLI >= ~0.129.** Exit code
  and wall time then exist only as a text header inside
  `function_call_output.output` (`Exit code: N\nWall time: X.Y seconds\n`).
  The parser reads both; `exit_code`/`duration_ms`/`output_bytes` are
  **first-writer-wins** in `apply_tool_result` so the precise exec event (old
  CLIs, arrives first) is never overwritten by the coarser parsed header.
- `custom_tool_call_output.output` is JSON **text**:
  `{"output": "...", "metadata": {"exit_code": 0, "duration_seconds": 0.4}}` —
  the apply_patch telemetry for files where `patch_apply_end` is missing.
- MCP calls arrive as plain `function_call` with the bare tool name; the
  telemetry (server, duration, `result: {"Ok": ...}|{"Err": ...}` — Rust enum
  serialization, plus `Ok.is_error`) comes from `event_msg/mcp_tool_call_end`.
  The result event upgrades the call's category to `mcp` via
  `ToolResultRec.category`.
- **Subagent threads are separate rollout files** (seen with CLI ~0.140):
  `session_meta.thread_source = "subagent"`, `source = {"subagent": {"other":
  "guardian"}}`, `parent_thread_id` = parent session id. They carry big token
  totals but **zero tool calls** (message-only review threads). Import links
  them via `sessions.parent_session_id`, mirrors them into `subagent_runs` on
  the parent and flags them `is_noise` to avoid double counting.
- `task_started` count > `task_complete` count when turns were interrupted
  (`turn_aborted`, `reason: "interrupted"`); `turn_count` uses MAX of both.
- Per-turn `input + cached` can exceed `model_context_window` when the turn
  spans a compaction — context pressure above 100% means forced compaction,
  not a math bug.
- `duration` is `{secs, nanos}` → ms.
- A rollout can contain **multiple `session_meta` lines** (resume).
- `response_item/message` with role `developer`/`user` duplicates `event_msg`
  user content — only the `assistant` role is ingested as an assistant message.

## SQLite

- `PRAGMA foreign_keys = ON` is per-connection (off by default); set in
  `connect()`. WAL is enabled there too.
- `PRAGMA user_version` and identifiers can't be bound as parameters — only feed
  them validated/derived values (migration number from filename, table name from
  a fixed allowlist).
- Generator pytest fixtures must be typed `Iterator[X]`, not `X`, or pyright
  errors.
- Schema changes during active development: no migration framework. Edit
  `db/migrations/0001_init.sql` in place and rebuild derived tables with
  `wdi import --full`. Preserve primary rows using a task-specific helper script
  before replacing a database; see `insights/AGENTS.md`.

## Incremental import

- Append-only files: only newline-terminated lines are final;
  `read_complete_lines` defers a trailing partial line to the next run.
- Resume by stored `byte_offset`. If the file shrank or mtime went backwards,
  do a **full reparse**: delete the stream's rows, then reinsert.
- Session aggregates are recomputed idempotently from the main stream, so
  re-running import never double-counts. Subagent-stream rows
  (`stream_id != 'main'`) are excluded from session-level tool/token metrics.

## Cross-machine snapshot merge

- Stable keys make merge idempotent (`INSERT OR IGNORE`): `session_id` and
  `agent_run_id` are global UUIDs.
- **`project_id` is a local autoincrement** — it differs per machine. Foreign
  sessions translate it by joining `projects.canonical_name`. Insert projects by
  name first.
- **`llm_analyses.analysis_id` is a local autoincrement** — do NOT copy it
  (two machines both have id 1). Insert without it; dedup relies on
  `UNIQUE(session_id, analysis_type, prompt_version)`.
- `import_state` is per-machine bookkeeping and is intentionally NOT merged.
- A machine skips its own `wdi-<machine_id>.sqlite` on import.

## FastAPI web server (`wdi serve`)

- **`from __future__ import annotations` + `Depends` under `TYPE_CHECKING` =
  silent 422.** FastAPI evaluates `Annotated[T, Depends(...)]` via
  `get_type_hints`; if `T` (e.g. `Settings`) is only imported under
  `TYPE_CHECKING`, resolution fails and the param is misread as a **required
  query param** → every request returns `422 {"loc":["query","settings"]}`.
  Import the dependency's parameter types at runtime in the deps module
  (`web/dependencies.py`).
- Modern Starlette signature: `templates.TemplateResponse(request, name, context,
  status_code=...)` — `request` is positional, not a `context["request"]` key.
- `Jinja2Templates`/`StaticFiles` need real directory paths; resolve package data
  with `Path(__file__).parent` (web assets) or `Path(str(resources.files(...)))`
  (reused report static). Chart.js is mounted from `wdi.report/static` at
  `/vendor`; htmx/CSS/JS are local under `web/static` at `/static`.

## Transcript segmenter & full I/O

- The DB stores only short previews (input 300 / output 500 / assistant 500 chars;
  user text 10000). Tool `input_preview`/`output_preview` are display-only — nothing
  else reads them. Full tool I/O and analyzer context read the original records
  on demand through `llm/transcript.py`. It resolves `wd://` keys through the
  Watchdog read contract and opens local JSONL for file-backed sessions.
- The web full view matches tool I/O by `call_id`. The full view and analyzer
  context mark missing source text as unavailable.
- Subagent transcripts resolve to `<sessionId>/subagents/agent-<streamId>.jsonl`
  next to the main file; `stream_id` in the DB equals the subagent `agent_run_id`.

## MCP server (`wdi mcp`)

- **FastMCP `@mcp.tool()` keeps the function sync** — the registered callable is
  reachable in tests via `server._tool_manager.get_tool(name).fn` and can be called
  directly (no event loop). `get_tool` returns `Tool | None`, so assert before use
  to satisfy pyright. `_tool_manager.list_tools()` is sync; `mcp.list_tools()` is async.
- Tools open a fresh connection per call via `connect()` (gives `PRAGMA foreign_keys`/
  WAL) and close it in a `contextmanager`. The server is strictly read-only — no
  writes, no schema change; it reuses `metrics.queries` + `SessionFilter`.
- `get_prompt_quality_report` reads pre-computed `llm_analyses` rows
  (`analysis_type='prompt_quality'`) only — no live LLM in the MCP process. The
  stored `result` column is a JSON string; parse it in the tool layer with
  `json.loads` wrapped in `contextlib.suppress(json.JSONDecodeError)` (ruff SIM105).
- The `mcp` SDK pulls a sizeable dependency tree (pydantic-settings, sse-starlette,
  cryptography, pywin32, …) — expected, it is a full protocol implementation.
- `fastapi.testclient.TestClient` needs `httpx` (dev dep). It raises server-side
  exceptions by default, so test 400/404 explicitly rather than the 500 handler.
- Reused presentation helpers live in `report/charts.py` (`chart_data`,
  `embed_json`, `register_jinja_filters`) so the static report and the server
  share one source.

## Misc

- **English-only contract** (CLAUDE.md): every committed file — code comments,
  docstrings, CLI help/strings, README, CHANGELOG — is English. Chat with the
  user stays Russian. An early assumption that UI strings stay Russian was wrong.
- `asdict()` on a slotted dataclass does **not** include `@property` values; pass
  the object itself to templates when you need computed fields
  (e.g. `Overview.cache_hit_rate`).
- Jinja2 JSON island: replace every `<` with its unicode escape (backslash u
  003c) before embedding chart data in a `<script type="application/json">`, so a
  `</script>` in the data can't break out (see `report/html.py:_embed_json`).
  Autoescape is on for HTML output.
- `wdi init` creates a **default** config pointing at the real user session
  dirs. Tests that exercise the create path must monkeypatch
  `wdi.ingest.service.run_import` so they don't read real sessions or write to
  the real `~/.wdi` database.
## Claude tool results stored beside the transcript

A Claude user message can contain a `tool_result` block whose string content
starts with `<persisted-output>`. Its header gives an absolute path before a
short preview. A synthetic example follows.

```text
<persisted-output>
Output too large (42.1KB). Full output saved to: C:\Users\example\.claude\projects\demo\session-id\tool-results\result-1.txt

Preview (first 2KB):
[preview]
</persisted-output>
```

Local transcripts confirmed that the filename can differ from the tool call ID.
Watchdog captures the file under `tool-results/<filename>` for the parent
session. The detail view validates the session directory in the reference,
looks up that relative path in the capture, and reads the bytes by digest.
When a file has several captured versions, the view uses the latest capture.
Missing side files are shown as unavailable. The separate `<output-file>`
marker in background task notifications can reference files outside that
session directory.
