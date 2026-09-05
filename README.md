# Watchdog

A local external observer for Codex and Claude Code coding sessions, built on top of their stock harnesses. One process per user, lightweight hook adapters, and one SQLite database per repository.

**Status:** design and Python foundation. Event collection, the background core, and hook installation are not implemented yet. See [ROADMAP.md](ROADMAP.md) for milestones, [TODO.md](TODO.md) for open work, and [DONE.md](DONE.md) for completed work.

## Agreed behavior

- Codex CLI and local coding sessions in ChatGPT desktop; Claude Code CLI and the local Code tab in Claude desktop. Ordinary chats, Cowork, and cloud/SSH sessions are outside the first release.
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
uv build
```

The package is `agent_watchdog` and the command is `agent-watchdog`, avoiding a collision with the `watchdog` filesystem monitoring library. Package publication is not currently planned.

All repository text must be written in English. Rules: [AGENTS.md](AGENTS.md). Contracts: [docs/architecture.md](docs/architecture.md). Sources: [docs/integrations.md](docs/integrations.md).

## Limitations

Hooks do not guarantee visibility into every tool. Transcripts are a supplementary source with an unstable format. Unknown metrics remain unknown; tool success or missing events alone do not prove progress or a stall.

Content remains local. Redacting known secrets does not guarantee anonymization: review exports before sharing them manually. Watchdog does not automatically change models, permissions, or instructions in monitored projects.

License: [MIT](LICENSE).
