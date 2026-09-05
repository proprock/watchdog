# Watchdog

A local external observer for Codex coding sessions, built on top of the stock harness. Claude Code support is a later independent milestone. One process per user, lightweight hook adapters, and one SQLite database per repository.

**Status:** A local [background core](docs/daemon.md), [metadata-only Codex hooks with an explicit installer](docs/hooks.md), validated configuration and registry ([API](docs/contracts.md)), and SQLite project storage ([storage](docs/storage.md)). Project CLI, content capture/redaction, retention, and analysis remain planned. See [ROADMAP.md](ROADMAP.md), [TODO.md](TODO.md), and [DONE.md](DONE.md).

## Agreed behavior

- M1-M4 target only Codex CLI and local coding sessions in ChatGPT desktop. Ordinary chats are outside product scope; Cowork and cloud/SSH sessions are excluded. Claude Code CLI and desktop Code are deferred to M-Anthropic, after WD-014 and before WD-015.
- The first hook starts an independent core that runs until logout or an explicit stop; a subsequent hook restarts it after a crash. MCP is not required.
- Collection is enabled for selected repositories. Worktrees share a database; separate clones have separate databases. Data lives outside working copies.
- Observation comes first: repetitions, errors, validation, duration, tool calls, compaction, available token counts, and output sizes. CLI and Markdown/JSON reports; a cross-project overview comes later.
- Session labels and export of candidate benchmark tasks. Users perform LLM analysis manually outside the product; there are no automatic LLM calls or eval runner.
- Content is retained for 30 days and metrics/labels for 180 days; retention and quotas are configurable. Pinned sessions are not deleted automatically but count toward quotas.
- Intervention and optional Codex App Server integration belong to later milestones.

## Development

Python 3.12+ and uv; the commands are identical in PowerShell and POSIX shells:

```console
uv sync --locked
uv run agent-watchdog --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
```

The package is `agent_watchdog` and the command is `agent-watchdog`, avoiding a collision with the `watchdog` filesystem monitoring library. Package publication is not currently planned.

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
