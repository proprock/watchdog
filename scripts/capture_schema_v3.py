"""Upgrade existing capture.sqlite3 stores from schema 1 or 2 to schema 3.

Stop Watchdog and other store users, then back up the stores before running:
    uv run python scripts/capture_schema_v3.py PATH/TO/capture.sqlite3 [...]

Each database is migrated in its own transaction. Its existing manifest is
updated after the commit. Rerun the command if that manifest update fails.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from agent_watchdog.capture_store import (
    CAPTURE_ENV_SCHEMA,
    CAPTURE_V3_SCHEMA,
    STORE_MANIFEST_FILENAME,
)
from agent_watchdog.files import atomic_write, open_shared_read


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        with open_shared_read(path) as stream:
            manifest = json.load(stream)
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise ValueError(f"invalid store manifest {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"expected a JSON object in store manifest {path}")
    version = manifest.get("capture_schema")
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError(f"expected capture_schema 1, 2 or 3 in store manifest {path}")
    return manifest


def _migrate_store(path: Path) -> bool:
    if not path.is_file():
        raise FileNotFoundError("capture database does not exist or is not a file")
    manifest_path = path.with_name(STORE_MANIFEST_FILENAME)
    uri = path.absolute().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, 3):
            raise ValueError(f"unsupported capture schema {version}; expected 1, 2 or 3")
        manifest = _read_manifest(manifest_path)
        if version == 1:
            for statement in CAPTURE_ENV_SCHEMA:
                db.execute(statement)
        if version in (1, 2):
            for statement in CAPTURE_V3_SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA user_version = 3")

    if manifest is not None and manifest["capture_schema"] in (1, 2):
        manifest["capture_schema"] = 3
        manifest["written_at"] = datetime.now(UTC).isoformat()
        content = (json.dumps(manifest) + "\n").encode("utf-8")
        try:
            atomic_write(manifest_path, content, allow_open_readers=True)
        except OSError as error:
            raise OSError(
                f"database is at schema 3; could not update {manifest_path}: {error}. "
                "Resolve the error and rerun to update the manifest."
            ) from error
    return version in (1, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stores", nargs="+", type=Path, metavar="capture.sqlite3")
    args = parser.parse_args()
    exit_code = 0
    for path in args.stores:
        try:
            migrated = _migrate_store(path)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"{path}: {error}", file=sys.stderr)
            exit_code = 1
        else:
            result = "upgraded to schema 3" if migrated else "already at schema 3"
            print(f"{path}: {result}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
