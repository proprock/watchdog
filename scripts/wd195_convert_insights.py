"""Back up and convert an Insights revision 2 database to revision 3."""

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("backup", type=Path, help="new backup file, which must not exist")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database must be an existing file")
    if args.backup.exists():
        parser.error("backup already exists; choose a new path")
    schema_path = (
        Path(__file__).resolve().parents[1] / "insights/src/wdi/db/migrations/0001_init.sql"
    )
    schema = schema_path.read_text(encoding="utf-8")
    projection = schema.split("-- Derived hook data, scoped to the originating store.", 1)[1].split(
        "-- Primary.", 1
    )[0]
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=rw", uri=True) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            parser.error("database must have schema revision 2")
        with sqlite3.connect(args.backup) as backup:
            db.backup(backup)
        db.executescript("BEGIN IMMEDIATE;\n" + projection + "\nPRAGMA user_version = 3;\nCOMMIT;")
    print(f"Converted {args.database}; backup: {args.backup}")


if __name__ == "__main__":
    main()
