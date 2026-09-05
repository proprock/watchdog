# Completed tasks

Historical records only. Read this file when prior verification is relevant; active work belongs in [TODO.md](TODO.md). Preserve task IDs when moving entries here.

- [x] **WD-001 - Foundation (2026-09-05).** README/AGENTS/CLAUDE, ROADMAP, architecture/integration contracts, uv package/lock, CLI help, and CI matrix. Local Windows/Python 3.12.13: `uv run pytest -q` reported 2 passed (initial run had a cache warning); Ruff check/format, `uv build`, and CLI help passed. The pytest rerun with an accessible cache and documentation checks are recorded in [verification](docs/verification.md). macOS/Linux/Windows CI is configured but has not run remotely; live hooks have not been tested.
- [x] **WD-017 - English repository text and separate completion history (2026-09-05).** Translated repository documentation into English, made English mandatory in AGENTS.md, and moved completed tasks out of TODO.md into DONE.md. Verification: reviewed translated contracts and task IDs; checked repository text for remaining Cyrillic, local Markdown links, UTF-8/LF, whitespace, and `git diff --check`. No runtime behavior changed.
