"""Open SQLite databases using the current Insights schema."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

# Increment whenever 0001_init.sql changes.
SCHEMA_REVISION = 2


class SchemaRevisionError(RuntimeError):
    """The database requires an explicit rebuild."""


def _check_schema(conn: sqlite3.Connection) -> None:
    revision = conn.execute("PRAGMA user_version").fetchone()[0]
    if revision == SCHEMA_REVISION:
        return

    if revision == 0:
        # Recheck under the write lock so concurrent openers initialize only once.
        conn.execute("BEGIN IMMEDIATE")
        revision = conn.execute("PRAGMA user_version").fetchone()[0]
        if revision == 0 and conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None:
            schema = resources.files("wdi.db") / "migrations" / "0001_init.sql"
            conn.executescript(schema.read_text(encoding="utf-8"))
            # PRAGMA does not support bound parameters. This is a source constant.
            conn.execute("PRAGMA user_version = {:d}".format(SCHEMA_REVISION))  # noqa: UP032
            revision = SCHEMA_REVISION
        if revision == SCHEMA_REVISION:
            conn.execute("COMMIT")
            return

    raise SchemaRevisionError(
        f"Insights database schema revision {revision} does not match {SCHEMA_REVISION}. "
        "Run insights/scripts/rebuild_db.py to rebuild it and preserve primary data."
    )


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a current database or create its schema in an empty database."""
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    # Avoid executescript's implicit commit in legacy transaction mode while setting up.
    conn = sqlite3.connect(db_path, autocommit=True, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _check_schema(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
    except BaseException:
        conn.close()
        raise
    return conn
