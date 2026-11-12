# Watchdog

A local external observer, and through approved [rules](docs/rules.md) a controller, for Claude Code and Codex coding sessions, built on top of the stock harnesses. Both providers have observation. Control is gated per provider, surface, exact version, event, tool and action by live evidence: Claude Code has the broader confirmed set, and Codex CLI has the exact Windows profiles of WD-151; Codex desktop control is unprobed (WD-153). One process per user, lightweight hook adapters, and one SQLite database per repository.

**Status:** A local [background core](docs/daemon.md), [Codex and Claude observation hooks with an explicit installer](docs/hooks.md), validated configuration and registry ([API](docs/contracts.md)), and [SQLite storage with retention, quotas, and redaction](docs/storage.md). Project registration, doctor, session inspection, and read-only shadow reports are available through the [CLI](docs/cli.md). `insights` (WD-123, WD-133, WD-134) sends a redacted evidence bundle to one isolated analysis call only when you run it, and returns grounded recommendations. The analysis backend, model and effort are selected explicitly and independently of the observed provider (WD-166): the isolated `claude -p` backend is the default and the only available one; the Codex backend reports `isolation_unavailable` and makes no model call ([WD-166a](docs/evidence/wd166a.md)). Codex rollout-v1 enrichment records reconciled usage, execution outcomes (WD-164) and model/effort/subagent attribution (WD-165). Claude observation (WD-022a/b) is complete. Control runs through the daemon's decision channel (WD-140) with declarative and approved Python rules (WD-142, WD-143); Codex CLI control is limited to the WD-151 profiles, and delivery stages are reported separately from provider effect (WD-168). Planned and not implemented: rule synthesis (WD-144), the automatic semantic judge (WD-145) and the calibration/promotion workflow (WD-147); their specification reviews are accepted. See [ROADMAP.md](ROADMAP.md), [TODO.md](TODO.md), and [DONE.md](DONE.md).

## Agreed behavior

- Scope is local coding sessions in Claude Code (CLI and desktop Code) and Codex (CLI and ChatGPT desktop). Control is enabled only for provider/surface/version/event/tool/action cells confirmed by live evidence ([matrix](docs/provider-compatibility.md)); unconfirmed cells stay observation-only. Ordinary chats are outside product scope; Cowork and cloud/SSH sessions are excluded.
- The first hook starts an independent core that runs until logout or an explicit stop; a subsequent hook restarts it after a crash. MCP is not required.
- Watchdog registers eligible local Git checkouts on first observation by default. Use `trusted_projects_dir` or `collect_exclude` to narrow automatic registration, and `agent-watchdog doctor` to see ignored record counts. Worktrees share a database. Separate clones have separate databases. Data lives outside working copies. See the [collection contract](docs/contracts.md#automatic-registration-and-ignored-records).
- Observation comes first: repetitions, errors, validation, duration, tool calls, compaction, available token counts, and output sizes. CLI and Markdown/JSON reports; a cross-project overview comes later.
- Session labels and export of candidate benchmark tasks. There is no eval runner. Model calls today are user-run (`insights`). The planned daemon judge (WD-145, not implemented) may call a model only for a project that enabled it, within a daily call budget and a timeout; tests and CI never call a model.
- Data is kept by default: no expiry and no quota (`content_days`, `metrics_days`, `project_bytes` are 0). Retention is a disk-budget control, not a privacy control: set positive values to expire data or enforce a quota. Pinned sessions are not deleted automatically but count toward quotas.
- Registered projects capture prompt/tool/assistant text by default; the local store keeps the raw provider input. Set `capture_content = false` globally or in project overrides to collect metadata only. The credential filter is a bounded pattern matcher applied to exports, not a guarantee that arbitrary secrets are detected; WD-115 moves it off the ingest path.
- Hooks remain the observation path. WD-015 found no documented way for Codex App Server to take ownership of arbitrary running CLI or desktop sessions, so no App Server adapter was added.
- All event producers use one durable schema 2 spool before the daemon commits to each project's SQLite Store. Native hooks publish raw inputs; developer hooks and daemon control events publish prepared envelopes. Queue files are acknowledged only after commit, preserving replay identity through a crash. See [storage and recovery](docs/storage.md#delivery-migration-and-recovery).

## Development

Python 3.14+, uv, and Rust stable with the platform linker/build tools. The commands are identical in PowerShell and POSIX shells:

```console
uv sync --locked
cargo fmt --manifest-path native/Cargo.toml --check
cargo clippy --locked --manifest-path native/Cargo.toml -- -D warnings
cargo test --locked --manifest-path native/Cargo.toml
uv run agent-watchdog --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
```

`uv run pytest` runs in parallel (`pytest-xdist`, `-n auto`) with a 180 s per-test timeout
(`pytest-timeout`); pass `-n 0` to run serially when debugging. Tests that drive the native
adapter share one session fixture, `rust_adapter`, which uses `WATCHDOG_NATIVE_ADAPTER` when
set, otherwise runs `cargo build --release --locked` (a no-op when current), and otherwise
skips with a message. Those tests carry the `native` marker, so `-m "not native"` runs the
Python-only part. CI sets `WATCHDOG_REQUIRE_NATIVE=1`, which turns that skip into a failure.

The default pytest suite excludes host-shell diagnostics. Run them explicitly with
`uv run pytest -m platform_diagnostic tests/platform_diagnostics` when diagnosing the
current machine's shell setup.

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
