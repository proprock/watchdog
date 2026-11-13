# WD-198 pricing and CLI migration implementation plan

**Goal:** Keep one pricing implementation in Insights and move `usage`, `overview`, and manual review `export` from Watchdog to `wdi`, preserving the reviewed output contract.

**Architecture:** `wdi` owns pricing and report construction. Watchdog remains the collector and control owner and supplies read-only source data through `readapi`. Hook-fact reports preserve the old event-time windows and missing-value semantics; transcript/session statistics remain a separate existing view.

**Tech stack:** Python 3.14, SQLite, Decimal, dated TOML tariffs, existing Pydantic/YAML settings and Typer CLI, pytest.

## Scope and approval gate

The prerequisite WD-195 is complete. Work started from the active development branch `feature/wd-223-codex-subagent-tokens` at `af7fe9a`, in the current checkout on `feature/wd-198-pricing-cli`. No worktree is needed.

The field inventory in [wd198.md](../evidence/wd198.md) was approved by the user on 2026-11-13 ("Да"). The migration drops no output fields; an existing storage column is not evidence that its replacement command is implemented. Removal may proceed after replacement and parity checks within that approved scope.

Acceptance requires both offline suites, the applicable static/build checks, and one fixed live week's token and cost comparison with the old command. The initial private token baseline covers `[2026-11-06T00:00:00Z, 2026-11-13T00:00:00Z)`; the live installation has no tariff configured, so that capture does not establish cost parity. Keep the task in TODO until all deliverables are complete.

Non-goals: moving insights modes (WD-199), changing raw telemetry, changing daemon/control behavior, new dependencies, or substituting current session statistics for hook-fact aggregates.

## Decisions

- Move the existing dated-tariff parser and Decimal arithmetic to `insights/src/wdi/metrics/pricing.py`, then add longest-prefix matching and the optional 5-minute cache-write rate. Do not keep a second estimator in Watchdog or in a CLI wrapper.
- Generate packaged default TOML rates from the current `DEFAULT_PRICING` values before removing that table. Test a fixed token vector against independently frozen old results for all seven current model keys. Reserved 1-hour/batch metadata must not discount ordinary usage. A named tariff file replaces the default completely; no missing-model fallback to packaged rates.
- Keep a compatibility path for existing explicit inline `Settings.pricing` values by normalizing them into the same tariff representation. A configured tariff file takes precedence. Do not retain a parallel arithmetic path or silently ignore existing custom prices.
- Resolve effective dates at each usage observation before aggregating. Pricing already aggregated model totals at today's rate would lose historical repricing. Preserve Codex-inclusive versus Claude-exclusive cached-input conventions at the source boundary and never subtract Codex cache twice.
- Use hook-event/fact snapshots for the migrated compatibility reports. Existing imported `hook_facts` are useful for analytics, but may lag collection or historical attribution refresh. The parity report must distinguish source snapshots from imported projections.
- Extend `readapi` with the narrowly required findings, project inventory, and session-review readers; expose unavailable schemas/data explicitly. Report construction and tariff arithmetic stay in `wdi`. Do not add a reverse dependency from Watchdog to Insights.
- Preserve the existing manual review export, including credential filtering, retention gaps, selected sessions, and labels. Existing `wdi export-snapshot` is database transfer and cannot replace that contract.

## Task 1: Contract inventory and approval

Files: `docs/evidence/wd198.md`, `TODO.md`.

1. Inspect current `inspection.usage`, `inspection.overview`, `inspection.export_sessions`, helpers, `facts_query`, pricing, CLI rendering, and `docs/cli.md`.
2. Map every nested field and output artifact to its existing or proposed source, including unavailable/error states and CLI options. Mark planned work explicitly.
3. Save a private pre-migration installed-command snapshot for the fixed week. Preserve the tariff bytes/hash if configured; otherwise record that the baseline is unpriced.
4. Present the inventory and request confirmation before deleting anything. Record the user's exact response. If fields are changed/dropped during implementation, renew this review before removal.

## Task 2: Single pricing engine (after approval)

Files: `insights/src/wdi/metrics/pricing.py`, `insights/src/wdi/config.py`, new packaged default TOML under `insights/src/wdi/`, `insights/tests/test_pricing.py`, `insights/tests/test_config.py`, relevant pricing tests moved from `tests/test_pricing.py`.

1. Add failing tests for dated section selection, historical repricing, prefix specificity within the selected section, before-earliest/unknown-model/missing-field reasons, invalid tariffs, Decimal precision, Codex missing-cache handling, 5-minute fallback including an explicit zero rate, and whole-file override semantics. Freeze old seven-model results independently of the replacement implementation.
2. Run `uv run --directory insights pytest tests/test_pricing.py tests/test_config.py`; confirm the intended behavioral failures. Commit only red tests with the WD-198 ID, named failures, and `TDD: red`, following AGENTS.md.
3. Implement the engine, generated default data, and settings normalization. Make every existing `PricingResolver` consumer delegate to that engine. Thread observation dates/provider conventions into consumers before aggregation; formatting boundaries may convert Decimal to the established presentation type.
4. Run the focused tests, then commit the solution with `TDD: green`. Check that the built Insights wheel includes the default tariff resource.

## Task 3: Read contract extensions

Files: `src/agent_watchdog/readapi.py`, `docs/read-contract.md`, `tests/test_readapi.py`; new Insights report tests.

1. Add failing tests for findings/source freshness, pending sessions, missing/old schemas, registered projects without databases, session labels, and review-export source data. Assert read-only access and explicit unavailable states.
2. Run `uv run pytest tests/test_readapi.py`; record red, implement the smallest bounded readers, and record green.
3. Document source columns, identity, snapshot/window semantics, and any deliberately allowed imports. Keep sanitization guarantees at the export boundary. Avoid schema changes when existing source records are sufficient.

## Task 4: Replacement commands without removing old ones

Files: new `insights/src/wdi/metrics/usage.py`, new `insights/src/wdi/metrics/overview.py`, new `insights/src/wdi/exporting.py`, `insights/src/wdi/cli.py`, new `insights/tests/test_usage.py`, `insights/tests/test_overview.py`, `insights/tests/test_exporting.py`, relevant CLI tests.

1. Move kept behavioral tests into Insights and add offline fixtures for every reviewed field group and error state. Cover provider/model/effort/attribution/conversation/turn/day grouping, process and coverage metrics, duplicate IDs across machines/projects, half-open event windows, and Decimal pricing provenance.
2. Cover all-project overview, independent per-project failures, schema gaps, materialized findings with pending/as-of state, and last activity. A time bound filters usage only, preserving the findings all-history contract.
3. Cover manual export artifacts, session selection and paging, labels, filtered credentials, retention/gap reporting, and refusal to overwrite a nonempty destination. No provider/network calls or writes to live configuration.
4. Run each new affected module to establish red; commit red tests, implement the three commands, then run those modules and commit green. Use one shared pricing engine for all costs, including current `stats`/report/web consumers.

## Task 5: Live parity and removal

Files: `src/agent_watchdog/cli.py`, `src/agent_watchdog/inspection.py`, `src/agent_watchdog/config.py`, removed `src/agent_watchdog/facts_query.py`, removed `src/agent_watchdog/pricing.py`, removed `scripts/benchmark_overview.py`, old-command tests, `docs/cli.md`, `docs/architecture.md`, `docs/read-contract.md`, relevant README/config examples.

1. Recheck the approval record. Run old and new usage over the same fixed live week, projects, counters, and tariff bytes. Since no live tariff is configured, generate a common explicit tariff from the frozen default rates and invoke the old command with it before deleting the old implementation.
2. Explain every token or cost difference, including prefix matches newly priced, cache conventions, source timing, missing counters, and rounding. Preserve private snapshots under ignored `tests/local/wd198`; commit only sanitized aggregates/provenance.
3. Search callers/imports before deleting the approved old commands and helpers. Keep `summary`, `report`, `telemetry`, `doctor`, `analyze errors`, labels, verdicts, rules, and all WD-199 modes functioning. Relocate any remaining shared helper instead of leaving a broken import.
4. Remove `Config.pricing` and the old `[pricing]` examples. In the same installed-instance update remove `[pricing]` from live configuration if present. Follow ignored `LIVE.md` exactly for a qualifying live update; do not publish private paths or mappings. If no update qualifies, record the reason and avoid claiming installation happened.

## Task 6: Verification and bookkeeping

1. Run focused tests as above during implementation. On the stable diff run `uv run pytest` and `uv run --directory insights pytest` as separate sessions.
2. Once on the stable diff run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv build --all-packages`, and `git diff --check`. No native changes are planned; if that changes, run the native checks in TENETS.md.
3. Audit red/green pairs with `python scripts/kaboost_audit_tdd.py <range> --run '<focused command with {tests}>'`, using the appropriate suite per pair. Inspect the complete diff and request the repository's required code review.
4. Record passed, failed, skipped, and unavailable checks separately. Quote the actual user confirmation and live comparison in `docs/evidence/wd198.md`.
5. Move WD-198 to one linked line in `DONE.md` only when acceptance is complete; otherwise retain it in `TODO.md` with the exact remaining work. Do not push or open a PR without authorization.
