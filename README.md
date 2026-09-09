# Watchdog

A local external observer for Codex coding sessions, built on top of the stock harness. Claude Code support is a later independent milestone. One process per user, lightweight hook adapters, and one SQLite database per repository.

**Status:** A local [background core](docs/daemon.md), [Codex and Claude observation hooks with an explicit installer](docs/hooks.md), validated configuration and registry ([API](docs/contracts.md)), and [SQLite storage with retention, quotas, and redaction](docs/storage.md). Project registration, doctor, session inspection, and read-only shadow reports are available through the [CLI](docs/cli.md). Codex rollout-v1 enrichment asynchronously records reconciled numeric usage with durable offsets; WD-010 adds versioned findings and debounced Git diff fingerprints. WD-022a adds a Claude observation adapter (CLI/desktop Code) and its live acceptance gate is met; Claude enrichment and guidance/control are WD-022b. See [ROADMAP.md](ROADMAP.md), [TODO.md](TODO.md), and [DONE.md](DONE.md).

## Agreed behavior

- M1-M4 target only Codex CLI and local coding sessions in ChatGPT desktop. Ordinary chats are outside product scope; Cowork and cloud/SSH sessions are excluded. Claude Code CLI and desktop Code observation is implemented in WD-022a (M-Anthropic); enrichment, lifecycle beyond observation, and guidance/control are WD-022b, after WD-014.
- The first hook starts an independent core that runs until logout or an explicit stop; a subsequent hook restarts it after a crash. MCP is not required.
- Collection is enabled for selected repositories. Worktrees share a database; separate clones have separate databases. Data lives outside working copies.
- Observation comes first: repetitions, errors, validation, duration, tool calls, compaction, available token counts, and output sizes. CLI and Markdown/JSON reports; a cross-project overview comes later.
- Session labels and export of candidate benchmark tasks. There are no automatic LLM calls or eval runner; sanctioned local analysis is opt-in.
- Content is retained for 30 days and metrics/labels for 180 days; retention and quotas are configurable. Pinned sessions are not deleted automatically but count toward quotas.
- Registered projects capture prompt/tool/assistant text by default after removing known credential forms. Set `capture_content = false` globally or in project overrides to collect metadata only. Redaction is a bounded pattern filter, not a guarantee that arbitrary secrets are detected.
- Intervention and optional Codex App Server integration belong to later milestones.

## Development

Python 3.12+, uv, and Rust stable with the platform linker/build tools. The commands are identical in PowerShell and POSIX shells:

```console
uv sync --locked
cargo build --release --locked --manifest-path native/Cargo.toml
cargo fmt --manifest-path native/Cargo.toml --check
cargo clippy --locked --manifest-path native/Cargo.toml -- -D warnings
uv run agent-watchdog --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
```

The package is `agent_watchdog` and the command is `agent-watchdog`, avoiding a collision with the `watchdog` filesystem monitoring library. Package publication is not currently planned.

The native adapter builds to `native/target/release/agent-watchdog-hook` (`.exe` on Windows).
Copy it to a stable location and select it during [hook installation](docs/hooks.md).
It is a separate host-specific binary; the Python wheel contains the portable core and fallback adapter.

To enable observation, follow [registration and hook installation](docs/hooks.md).
Installation defaults to dry-run and never changes native trust. Review hooks in
the Codex profile used by the target surface: trust saved under `--profile lean`
did not apply to desktop in the Windows probe; desktop required native review in
the base configuration. Do not copy trust records between profiles.

All repository text must be written in English. Rules: [AGENTS.md](AGENTS.md). Contracts: [docs/architecture.md](docs/architecture.md). Sources: [docs/integrations.md](docs/integrations.md).

## Limitations

Hooks do not guarantee visibility into every tool. Transcripts are a supplementary source with an unstable format. Unknown metrics remain unknown; tool success or missing events alone do not prove progress or a stall.

Content remains local. Redacting known secrets does not guarantee anonymization: review exports before sharing them manually. Watchdog does not automatically change models, permissions, or instructions in monitored projects.

License: [MIT](LICENSE).
