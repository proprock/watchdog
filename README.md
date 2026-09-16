# Watchdog

A local external observer for Codex coding sessions, built on top of the stock harness. Claude Code support is a later independent milestone. One process per user, lightweight hook adapters, and one SQLite database per repository.

**Status:** A local [background core](docs/daemon.md), [Codex and Claude observation hooks with an explicit installer](docs/hooks.md), validated configuration and registry ([API](docs/contracts.md)), and [SQLite storage with retention, quotas, and redaction](docs/storage.md). Project registration, doctor, session inspection, and read-only shadow reports are available through the [CLI](docs/cli.md). Codex rollout-v1 enrichment asynchronously records reconciled numeric usage with durable offsets; WD-010 adds versioned findings and debounced Git diff fingerprints. WD-022a adds a Claude observation adapter (CLI/desktop Code) and its live acceptance gate is met; Claude enrichment and guidance/control are WD-022b. See [ROADMAP.md](ROADMAP.md), [TODO.md](TODO.md), and [DONE.md](DONE.md).

## Agreed behavior

- M1-M4 target only Codex CLI and local coding sessions in ChatGPT desktop. Ordinary chats are outside product scope; Cowork and cloud/SSH sessions are excluded. Claude Code CLI and desktop Code observation is implemented in WD-022a (M-Anthropic); enrichment, lifecycle beyond observation, and guidance/control are WD-022b, after WD-014.
- The first hook starts an independent core that runs until logout or an explicit stop; a subsequent hook restarts it after a crash. MCP is not required.
- Collection is enabled for selected repositories. Worktrees share a database; separate clones have separate databases. Data lives outside working copies.
- Observation comes first: repetitions, errors, validation, duration, tool calls, compaction, available token counts, and output sizes. CLI and Markdown/JSON reports; a cross-project overview comes later.
- Session labels and export of candidate benchmark tasks. There are no automatic LLM calls or eval runner; sanctioned local analysis is opt-in.
- Content is retained for 30 days and metrics/labels for 180 days by default. Retention is a disk-budget control, not a privacy control: set the day counts to 0 to keep data until the project quota needs space. Pinned sessions are not deleted automatically but count toward quotas.
- Registered projects capture prompt/tool/assistant text by default; the local store keeps the raw provider input. Set `capture_content = false` globally or in project overrides to collect metadata only. The credential filter is a bounded pattern matcher applied to exports, not a guarantee that arbitrary secrets are detected; WD-115 moves it off the ingest path.
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

The native adapter is a separate host-specific binary and the only production hook
entry point (WD-110); the Python wheel also contains a Python-path CLI command kept
only as a developer/test utility, never installed by `hooks install`. Tagged releases
publish checked ZIP archives for Windows x86_64, Linux x86_64, and macOS arm64 (Intel
macOS is not a supported target: GitHub no longer offers a free Intel-hosted macOS
runner, and Apple Silicon has replaced Intel Macs). Their stable names are
`agent-watchdog-hook-v<VERSION>-<RUST-TARGET>.zip`; each archive carries a manifest
and the release includes `SHA256SUMS.txt`. Pass a downloaded archive to the hook
installer with `--adapter-artifact`; it verifies the checksum and host target and
copies the binary to a stable local location without requiring Rust. Building from
source still produces `native/target/release/agent-watchdog-hook` (`.exe` on Windows),
which can be selected directly during [hook installation](docs/hooks.md).

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
