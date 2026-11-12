"""SQLite connection factory and migration runner."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: Path | str) -> sqlite3.Connection:
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _load_migrations() -> list[tuple[int, str]]:
    migrations_dir = resources.files("wdi.db") / "migrations"
    migrations: list[tuple[int, str]] = []
    for entry in migrations_dir.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            migrations.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(migrations)


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns how many were applied."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = 0
    for number, sql in _load_migrations():
        if number <= current:
            continue
        conn.executescript(sql)
        # PRAGMA does not support parameter binding; number comes from filenames.
        conn.execute(f"PRAGMA user_version = {number:d}")
        conn.commit()
        applied += 1
    return applied
