Strictly follow the repository `TENETS.md` and the root `AGENTS.md`; this file adds package rules.

## Architecture
Keep FastAPI route handlers thin.
Put business logic in service/application modules, not in routes or templates.
Put SQLite access behind a small repository/query layer.
Do not open ad-hoc SQLite connections in handlers, templates, or tests.
Keep HTTP, domain logic, persistence, and rendering boundaries explicit.
Use FastAPI dependencies for request-scoped resources: settings, DB connection/session, current user, feature flags.
Avoid global mutable state except immutable config loaded at startup.

## SQLite rules
Enable foreign keys for every connection: PRAGMA foreign_keys = ON.
Use explicit transactions for writes.
No migration framework while in development. Edit `src/wdi/db/migrations/0001_init.sql` in place. Derived tables are rebuilt with `wdi import --full`. Primary tables are carried over by a helper script under `insights/scripts/`, run against the installed database and recorded in the task's evidence file. Never tell the user to delete a database that holds primary rows without running that script.
Never build SQL with f-strings or string concatenation.
Use parameters for all user-provided values.
Keep queries readable. Prefer named helper functions over large inline SQL blocks.
In tests, use an isolated temporary database per test or per test module.
Do not rely on test execution order.

## FastAPI rules
Use APIRouter by feature or surface area.
Use Annotated[..., Depends(...)] for dependencies.
Return templates for browser pages and JSON for chart/data endpoints.
Validate input at the boundary with Pydantic models, form parsing, or typed query params.
Use explicit status codes for non-200 responses.
Keep exception handling centralized.
Do not leak tracebacks, SQL errors, filesystem paths, secrets, or internal IDs to users.
For mutating form actions, use CSRF protection if cookies or sessions are involved.

## Jinja2 rules
Templates render presentation only.
Do not put business decisions, DB calls, or complex data shaping in templates.
Escape output by default.
Use macros for repeated UI fragments.
Keep full-page templates under templates/pages/.
Keep HTMX fragments under templates/partials/.
Name partials by target area, for example: _table.html, _filters.html, _chart.html

## HTMX rules
HTMX endpoints should return partial HTML, not whole pages.
Full-page endpoints should work without HTMX when practical.
Preserve filter state in query params.
Make filter requests idempotent and bookmarkable when they do not mutate state.
Check HX-Request only when the response differs for HTMX and normal navigation.
Return stable DOM targets and avoid fragile selectors.
After changing filters, update affected fragments only.
Do not duplicate filtering logic between Python and JavaScript.

## Charts
Prefer server-generated JSON data and thin client-side rendering.
Keep Chart.js/ECharts initialization idempotent.
Destroy or update existing chart instances before re-rendering.
Do not inline unsafe JSON through string concatenation.
Put chart code in static JS modules, not large inline template scripts.
Keep chart endpoints stable and documented by response shape.
Test chart data endpoints separately from visual rendering.

## Testing
Add or update tests for every behavior change.
Use pytest fixtures for app, client, DB, config, and dependency overrides.
Use FastAPI TestClient for HTTP behavior.
Use app.dependency_overrides to replace DB, auth, clock, external services, and config in tests.
Do not call real external services in tests.
Use tmp_path for files and temporary SQLite databases.
Test both normal page loads and HTMX fragment responses.
Test filtering, sorting, pagination, empty states, invalid input, and permission failures.
Prefer deterministic factories over shared fixtures with hidden state.

Run focused tests first, then the full suite:

```Bash
uv run --directory insights pytest tests/path/to/test_file.py -q
uv run --directory insights pytest
```

## Code style
**Please use English only for comments and add them only where the code is non-obvious or complex. The code is reviewed by a Lead Software Engineer.**
Use type hints for public functions, route handlers, service functions, and DB boundaries.
Prefer pathlib.Path over string paths.
Prefer datetime.UTC and timezone-aware datetimes.
Keep functions small enough to test directly.
Use clear names over comments.
Comments should explain why, not restate what.
Avoid broad except Exception unless the handler re-raises or logs with context.
Use structured logging where possible.
Do not print from application code.

### Security and privacy
Treat all request data as untrusted.
Parameterize SQL.
Escape rendered output.
Do not log secrets, tokens, cookies, personal data, or raw request bodies.
Do not commit .env, local databases, generated reports, caches, or credentials.
Validate uploaded filenames, paths, MIME types, and size limits.
Keep authorization checks server-side.

### Static assets
Keep first-party JavaScript under `insights/src/wdi/web/static/` or `insights/src/wdi/report/static/`.
Keep first-party CSS under `insights/src/wdi/web/static/` or `insights/src/wdi/report/static/`.
Do not introduce a frontend build step unless the task requires it.
Prefer progressive enhancement: pages should remain understandable without JavaScript.

## Done criteria

A change is done only when:

- implementation matches the requested behavior;
- tests cover the changed behavior;
- SQLite schema/data changes are reproducible;
- templates render valid states: normal, empty, invalid, error;
- HTMX responses update the intended fragment only;
- all relevant checks pass or failures are explained.

See `insights/GOTCHAS.md` for known issues and prior findings.
