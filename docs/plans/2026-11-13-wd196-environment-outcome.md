# WD-196 Environment and Repository Outcome Implementation Plan

**Goal:** Import captured environment/outcome metadata and show it on session pages with branch, repository-change and instruction-change filters.

**Architecture:** Extend the read contract with paged dictionary readers and on-demand blob bytes. Refresh small derived snapshot tables per originating machine/store during import, even when transcripts have not grown. A query/service layer shapes session panels, compares instruction/settings digests against the previous session of the same project and resolves blobs only through the originating store. Routes and templates remain thin.

**Tech Stack:** Python, SQLite, FastAPI, Jinja2, pytest; existing dependencies only.

**Criteria:** Preserve null statistics; distinguish no environment, no outcome, missing blob and capture errors. Show directory, commit, branch, model, file digests/change markers and rule set. Show observed repository statistics and HEAD movement without treating Stop as success. Scope source identities by machine/project/provider. Never execute trace content or copy blobs into Insights.

**Non-goals:** New capture behavior, model calls, pricing, changing collector installation, automatic success judgments, and recovering committed diffs by executing Git in recorded paths.

**Decision:** Retain source columns in derived tables plus `machine_id`, source `project_id` (TEXT), and `store_path`. Prefer full atomic snapshot metadata refresh over a timestamp cursor: captures may arrive late or be purged. Keep captured working-copy diff separate from HEAD movement; committed content is unavailable unless captured.

## Task 1: Read contract and derived import

- Files: `src/agent_watchdog/readapi.py`, `tests/test_readapi.py`, `insights/src/wdi/ingest/environment.py`, `insights/src/wdi/ingest/watchdog.py`, `insights/src/wdi/db/migrations/0001_init.sql`, `insights/src/wdi/db/connection.py`, `insights/tests/test_environment_import.py`, `docs/read-contract.md`.
- Add failing tests for metadata readers, old/absent capture schemas, on-demand blob access, repeat imports, late outcomes and source isolation. Run focused tests, commit red tests, then implement and verify green.
- Readers: `session_env(info)`, `checkout_states(info)` yield dictionaries with source column values (JSON remains serialized); `blob_bytes(info, digest)` returns bytes or raises KeyError for absent content.
- Derived tables contain every source column plus `machine_id`, `project_id`, `store_path`; composite primary keys include machine/project and the source key. Increment schema revision once. No blob table in Insights.

## Task 2: Panels and filters

- Files: new `insights/src/wdi/metrics/environment.py`, `insights/src/wdi/metrics/filters.py`, `insights/src/wdi/web/filters.py`, `insights/src/wdi/web/routes/sessions.py`, templates and `insights/tests/test_environment_web.py`.
- Write failing HTTP tests for normal, empty, unavailable, escaped diff content and filters (full pages and HTMX). Commit red tests before implementation.
- Shape environment/outcome through the query/service layer, compare file digests by path and category with the previous project session, and scope all joins to machine/provider/store.
- Load captured diffs on demand via a dedicated route. Preserve filters in URLs, show controls on the session list, and distinguish unknown from false.

## Task 3: Acceptance and bookkeeping

- Run focused tests then `uv run pytest` and `uv run --directory insights pytest`, Ruff checks/format, ty and `uv build --all-packages` once on the stable diff.
- Rebuild the installed Insights database with `insights/scripts/rebuild_db.py`, preserving primary data; perform `wdi import --full`. Record the result and inspect a live session via local HTTP/browser. Do not call providers.
- Review final diff, update user documentation and `docs/evidence/wd196.md`. Move WD-196 to DONE only when all acceptance deliverables pass. Commit green implementation with `TDD: green`; do not push.
