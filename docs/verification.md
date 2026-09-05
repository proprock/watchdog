# Foundation verification

2026-09-05, Windows, isolated CPython 3.12.13 via uv 0.11.7. For sandbox runs, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` point to the ignored project `.cache` directory; this is an environment workaround, not a product requirement.

| Check | Result |
|---|---|
| TDD red: pytest before package creation | Both tests failed because agent_watchdog was missing |
| `uv run pytest -q` | 2 passed; cache warning due to sandbox permissions |
| `uv run pytest -q -o cache_dir=.cache/pytest` | 2 passed, no warnings |
| `uv sync --locked` | PASS |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS |
| `uv build` | Wheel and sdist built |
| `uv run agent-watchdog --help` | PASS; explicitly states that collection is not implemented |
| `git diff --check` | PASS for the tracked diff |

The new text files were also checked for local Markdown links, trailing whitespace, and UTF-8/LF: 17 files, PASS. This was a documentation check, not a product test suite.

The CI matrix is configured; remote CI has not run. macOS/Linux and live vendor desktop/CLI hooks have not been tested. Fixtures or documentation alone do not establish runtime support. The next task is WD-002. Completed task records live in [DONE.md](../DONE.md).
