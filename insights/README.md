# watchdog-insights (wdi)

Local analytics for Claude Code and OpenAI Codex agent sessions: import JSONL
transcripts into a normalized SQLite database, compute quantitative metrics,
run LLM analysis, generate a static HTML report, or browse a local web dashboard.

## Install and run

```bash
uv sync --locked
uv run wdi --help
```

## Quick start

```bash
uv run wdi init            # create ~/.wdi/config.yaml and import all sources
uv run wdi stats           # terminal summary
uv run wdi report          # static HTML report
uv run wdi serve           # local web dashboard at http://127.0.0.1:8000
```

`wdi serve` runs a local web UI: a live dashboard with filters
(agent/project/since), a sessions list and per-session detail. Sessions are shown
by their real name (agent title, else the first prompt). Quality badges (with a
legend) flag slow, error-prone, oversearching, and high/low-scoring sessions and
subagents. The detail page surfaces stored LLM analysis (prompt quality, session
summary, agent efficiency) for sessions run through `wdi analyze`, and you can
drill into a session or subagent down to individual tool calls and their full
input/output (read on demand from the original transcript). Override the bind
address with `--host`/`--port` (defaults come from the `server` config block),
and pass `--import` to refresh the database before serving:

```bash
uv run wdi serve --port 8080 --import
```

### Context analysis

Open **Context Analysis** in the local dashboard and press **Run local analysis**
to rank long sessions by model calls, turns, cumulative input, peak context,
observable context size, compactions, or repeated content. The selected session
shows a per-call timeline: exact token counters use one axis, while stacked UTF-8
byte sizes of observable instructions, messages, code, search results, listings,
diffs, and logs use the other.

This is a reconstruction of data present in the session files, not a copy of the
provider's complete API request. Missing internal prompts and encrypted reasoning
or compaction state are marked `partial` or `opaque`; ciphertext length is never
treated as context size. WDI stores only categories, sizes, tool/command kinds,
and SHA-256 fingerprints in `context_events`, not duplicate raw content or paths.

The local advisor compares measurable compaction outcomes only after enough
events exist and provides a `compact_prompt` draft without changing Codex
configuration. Optional Claude and Codex buttons are the only actions that invoke
an external model; their payload contains aggregate counts, categories, and
ratios, with no raw text, commands, paths, or session identifiers.

See [Context structure analysis](docs/context-analysis.md) for reconstruction
semantics, evidence thresholds, privacy boundaries, and the import benchmark.

`wdi init` is sugar for `wdi config init` followed by `wdi import`. Run the
steps separately when you need to edit the config before the first import:

```bash
uv run wdi config init     # create ~/.wdi/config.yaml
uv run wdi import          # import Watchdog captures and provider files
```

`wdi import` reads Watchdog captures first, then imports any remaining sessions
from enabled Claude Code and Codex file sources. Insights stores the parsed
metrics and previews. The original captured transcript remains in Watchdog.

The same import incrementally copies hook events and facts, including stores
without a captured transcript. `wdi sessions show SESSION_ID` reports observed
permission waits, tool durations, and delivered control actions grouped by rule.
Missing or unfinished observations remain unavailable.

`wdi doctor` compares transcript and hook token usage for sessions present in
both sources, separately for main streams and subagents. It reports input,
cached-input, cache-write, and output differences above 1% of Watchdog usage.
Totals include only pairs with known values on both sides; unavailable pairs
are counted separately. This reports differences without correcting either
accounting convention. Older model/effort attribution backfills require
`wdi import --full` to refresh previously imported facts (WD-220).

`watchdog.data_dirs` in the YAML config defaults to the platform's Watchdog
data directory. Set it to a list of paths when the installed Watchdog uses a
custom data directory. Run `uv run wdi doctor` to see the discovered stores,
unsupported contracts, session counts by origin, and file-only counts per
provider. Once a provider's file-only count has been zero for a week, disable
its file source with `sources.claude_code.enabled: false` or
`sources.codex.enabled: false`, as appropriate.

## Database maintenance

For the WD-195 revision 2 to 3 conversion, close Insights clients and run from
the repository root, choosing a backup path that does not exist:

```bash
uv run python scripts/wd195_convert_insights.py /path/to/wdi.sqlite /path/to/backup.sqlite
uv run --directory insights wdi import
```

The one-off converter backs up the database and adds only the derived hook
tables. Existing sessions and paid analyses are retained.

The default database is `~/.wdi/wdi.sqlite`. If WDI reports a schema revision
mismatch, close all Insights clients. From the `insights/` directory, run:

```bash
uv run python scripts/rebuild_db.py
uv run wdi import --full
```

The rebuild retains paid analyses and coach reports, legacy sessions and their
transcript rows, and the parent sessions required by analyses. It keeps a
timestamped SQLite backup next to the rebuilt database.

To bring data over from the legacy Insights database, finish a full import into
the new database first. Then close the legacy application and run:

```bash
uv run python scripts/import_legacy_db.py --from /path/to/old.sqlite
```

This adds missing sessions as `legacy` and copies paid analyses and coach
reports. Repeating the command skips records already copied. If a paid record
shares a key with a different destination record, the import reports the
conflict and rolls back. Provider imports preserve legacy sessions and their
transcript rows.

## Model pricing

Set `pricing` in `~/.wdi/config.yaml`. All rates are USD per million tokens;
model names match by their longest configured prefix, including dated variants.
For example:

```yaml
pricing:
  claude-opus-5:
    input: 5.0
    output: 25.0
    cache_read: 0.50
    cache_write_5m: 6.25
    cache_write_1h: 10.0
    batch_input: 2.50
    batch_output: 12.50
```

`input` and `output` are required for each configured model. `cache_read` defaults
to zero. The four extended cache and Batch fields are optional (`null` means
unspecified). Existing `cache_write` settings remain supported: cost estimates use
`cache_write_5m` when specified, otherwise `cache_write` (which defaults to zero).

Current session aggregates do not distinguish cache lifetimes or Batch usage.
Cache creation is therefore estimated at the 5-minute rate; `cache_write_1h`,
`batch_input`, and `batch_output` are stored for future accounting and do not yet
affect reports. One-hour cache use can consequently be underestimated.

After editing prices, rerun `uv run wdi stats` or `uv run wdi report`; no session
re-import is needed. Restart `wdi serve` to reload its configuration. New defaults
apply to newly created configs; existing config files are not rewritten.

## MCP server

`wdi mcp` runs a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio so an agent can query the same analytics. It exposes four tools,
each accepting optional `agent`, `project`, `since` (`2026-06-01` or `7d`) and
`limit` arguments:

- `get_recent_sessions` — recent sessions with tokens and tool-call counts.
- `get_tool_failure_patterns` — tools that errored, ranked, with `error_rate`.
- `get_oversearching_examples` — search-heavy / edit-light sessions and redundant re-reads.
- `get_prompt_quality_report` — stored prompt-quality scores (worst first).
  Run `wdi analyze prompt-quality` first; un-analyzed sessions are omitted.

Register it with an MCP client (e.g. Claude Code / Claude Desktop):

```json
{
  "mcpServers": {
    "watchdog-insights": {
      "command": "wdi",
      "args": ["mcp"]
    }
  }
}
```

Source data formats are described in [docs/](docs/).

## Dashboard coaching

The Coach section has independent Claude and Codex cards, each with its own model
selector, run/refresh button, loading indicator and result. Changing a model only
loads its saved advice. Running a card sends aggregate metrics (including frequently
read file paths) for the active dashboard filters to that provider's CLI. It does
not send full session transcripts. Both CLIs must be installed and signed in for
their respective cards to work; model availability depends on the account.

Model menus can be customized in `~/.wdi/config.yaml`:

```yaml
features:
  llm_analysis:
    enabled: true
    coach_models:
      claude: [haiku, sonnet, opus]
      codex: [gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna]
```

By default the Codex executable is resolved from the server's PATH. If several
CLI installations coexist, set `features.llm_analysis.codex_executable` to the
full path of the desired executable. This is useful when an older npm CLI is
found before the newer CLI bundled with the Codex app.

The Claude card initially uses `models.coach` (or the shared `model` default),
which is included in its menu. Codex initially uses the first configured Codex
model. Restart the server after editing the menus. Selecting a different model
affects that card only; reloading the dashboard restores the configured defaults.

Advice is cached separately by provider, model, filters and prompt version.
Saved metrics fingerprints detect changed data or prices: outdated advice is
marked on display and recomputed on the next run. Refresh always requests a new
answer. Existing single-provider cache rows are retained but are not reused.
No database schema change or re-import is required.

`wdi analyze` selects its engine through `features.llm_analysis.backend` in
`~/.wdi/config.yaml`. Use `claude` (the default) or `codex`. Existing
`claude-cli` settings select Claude.

```yaml
features:
  llm_analysis:
    backend: codex
    model: gpt-5.6-sol
```

`--model` overrides the configured model for one invocation. Per-analyzer
settings under `models` take precedence over `model`. If `model` is left at
the Claude default `haiku`, Codex uses the first model in `coach_models.codex`.
Set `codex_executable` if the CLI needs an explicit path. On Windows, use a
native `codex.exe`. Use `--force` to regenerate analyses already saved for
the same sessions. The Coach and Context Advice cards select their engine
through the provider-specific buttons.

Claude advice and Claude-backed `wdi analyze` use Watchdog's isolated runner. It uses
the CLI's existing authentication and runs in a temporary directory outside
registered checkouts. Safe mode, empty setting sources, strict MCP configuration
and an empty tool list disable customizations and tools. Session persistence is
disabled. The runner refuses the request if it cannot establish isolation.

Codex runs through `codex exec` with stdin input and an ephemeral session,
using a temporary working directory and the CLI's existing
authentication. It uses a read-only sandbox and ignores user configuration so
project-specific settings do not influence the analysis. Its analysis sessions
are not saved to the session files imported by WDI. This path provides weaker
isolation than the Claude runner. Watchdog's own Codex analysis backend remains
unavailable pending a verified isolation contract (WD-166).

### Context efficiency

Use **Context Analysis** to compare filtered, Top N, or selected sessions, inspect
observable context growth and reconciled token usage, preview aggregate-advisor
API-equivalent prices, and compare local cost scenarios. See the
[context efficiency audit](docs/context-efficiency-audit.md) for the local findings,
rebuild instructions, and opt-in native compaction experiments.
