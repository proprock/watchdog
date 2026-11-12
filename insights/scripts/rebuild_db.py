"""Rebuild the Insights schema after closing every Insights client."""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from _db_transfer import (
    TransferError,
    database_arguments,
    database_path,
    print_counts,
    readonly_source,
    sidecar,
    source_state,
    transfer,
    verify_source,
)
from wdi.config import ConfigError
from wdi.db.connection import SchemaRevisionError, connect


def _verify_closed(path: Path) -> None:
    if any(sidecar(path, suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise TransferError(
            "SQLite sidecar files are present. Close all Insights clients and retry. "
            "If a client crashed, open and close the database in SQLite to recover its journal."
        )


def _backup(uri: str, backup: Path) -> None:
    with (
        closing(sqlite3.connect(uri, uri=True)) as source,
        closing(sqlite3.connect(backup)) as destination,
    ):
        source.execute("PRAGMA foreign_keys = ON")
        destination.execute("PRAGMA foreign_keys = ON")
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise TransferError("The backup failed SQLite's integrity check.")


def _rebuild(path: Path) -> tuple[Path, dict[str, int]]:
    if not path.is_file():
        raise TransferError("The Insights database does not exist. Check --db or --config.")
    _verify_closed(path)
    state = source_state(path)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    with (
        readonly_source(path, state) as uri,
        tempfile.TemporaryDirectory(prefix=".wdi-rebuild-", dir=path.parent) as directory,
    ):
        replacement = Path(directory) / "rebuilt.sqlite"
        staged_backup = Path(directory) / "backup.sqlite"
        _backup(uri, staged_backup)
        with closing(connect(replacement)) as conn:
            counts = transfer(conn, uri, importing=False)
            if conn.execute("PRAGMA main.quick_check").fetchone()[0] != "ok":
                raise TransferError("The replacement failed SQLite's integrity check.")
            conn.execute("COMMIT")
        _verify_closed(replacement)
        verify_source(path, state)
        _verify_closed(path)
        staged_backup.replace(backup)
        replacement.replace(path)
    return backup, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    database_arguments(parser)
    try:
        backup, counts = _rebuild(database_path(parser.parse_args()))
    except (ConfigError, OSError, sqlite3.Error, SchemaRevisionError, TransferError) as exc:
        parser.exit(1, f"Database rebuild failed: {exc}\n")
    print(f"Backup saved to {backup}")
    print_counts(counts)
    print("Run wdi import --full to rebuild derived data.")


if __name__ == "__main__":
    main()
