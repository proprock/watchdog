"""Import legacy sessions and paid data after a full Insights import."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from _db_transfer import (
    TransferError,
    database_arguments,
    database_path,
    print_counts,
    readonly_source,
    source_state,
    transfer,
    verify_source,
)
from wdi.config import ConfigError
from wdi.db.connection import SchemaRevisionError, connect


def _validate_paths(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise TransferError("The legacy database file does not exist. Check --from.")
    if source == destination or (destination.exists() and source.samefile(destination)):
        raise TransferError("Choose a different destination database with --db or --config.")
    home = Path.home().resolve()
    old_directory = source.parent
    if (
        old_directory.parent == home
        and old_directory.name.startswith(".")
        and old_directory.name != ".wdi"
        and destination.is_relative_to(old_directory)
    ):
        raise TransferError("The destination must be outside the legacy database directory.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    database_arguments(parser)
    parser.add_argument("--from", dest="source", type=Path, required=True, help="Legacy database")
    args = parser.parse_args()
    try:
        source = args.source.expanduser().resolve()
        destination = database_path(args)
        _validate_paths(source, destination)
        state = source_state(source)
        with readonly_source(source, state) as uri, closing(connect(destination)) as conn:
            counts = transfer(conn, uri, importing=True)
            verify_source(source, state)
            conn.execute("COMMIT")
    except (ConfigError, OSError, sqlite3.Error, SchemaRevisionError, TransferError) as exc:
        parser.exit(1, f"Legacy import failed: {exc}\n")
    print_counts(counts)


if __name__ == "__main__":
    main()
