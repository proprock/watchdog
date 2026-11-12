"""SQLite row transfer shared by the one-off Insights scripts."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wdi.config import load_settings

_DEPENDENTS = ("messages", "tool_calls", "context_events", "subagent_runs")
_SESSION_KEYS = {"session_id", "machine_id", "project_id", "agent", "origin"}


class TransferError(Exception):
    """The database cannot be transferred safely."""


def database_arguments(parser: argparse.ArgumentParser) -> None:
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--db", type=Path, help="Insights database path")
    options.add_argument("--config", type=Path, help="Insights configuration path")


def database_path(args: argparse.Namespace) -> Path:
    path = args.db if args.db is not None else load_settings(args.config).db_path
    path = path.expanduser().resolve()
    if path.is_relative_to((Path.home() / ".asi").resolve()):
        raise TransferError("Choose an Insights database outside the old application directory.")
    return path


def sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def file_state(path: Path) -> tuple[int, ...]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def source_state(path: Path) -> tuple[tuple[int, ...], ...]:
    return tuple(file_state(candidate) for candidate in (path, sidecar(path, "-wal")))


def verify_source(path: Path, expected: tuple[tuple[int, ...], ...]) -> None:
    journal = file_state(sidecar(path, "-journal"))
    if source_state(path) != expected or (journal and journal[2]):
        raise TransferError("The source is changing. Close all source database clients and retry.")


@contextmanager
def readonly_source(path: Path, state: tuple[tuple[int, ...], ...]) -> Iterator[str]:
    """Open a read-only source without creating sidecars in its directory."""
    verify_source(path, state)
    _, wal = state
    if not wal or not wal[2]:
        yield path.as_uri() + "?mode=ro&immutable=1"
        return
    with tempfile.TemporaryDirectory(prefix="wdi-source-") as directory:
        snapshot = Path(directory) / "source.sqlite"
        try:
            shutil.copyfile(path, snapshot)
            shutil.copyfile(sidecar(path, "-wal"), sidecar(snapshot, "-wal"))
        except OSError:
            verify_source(path, state)
            raise
        verify_source(path, state)
        # SQLite may create SHM in the private snapshot directory to read its WAL.
        yield snapshot.as_uri() + "?mode=ro"


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> tuple[str, ...]:
    return tuple(
        row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?, ?)", (table, schema))
    )


def _identifier(name: str) -> str:
    return '"{}"'.format(name.replace('"', '""'))


def _copy(
    conn: sqlite3.Connection,
    table: str,
    selection: str,
    *,
    override: dict[str, str] | None = None,
    only: set[str] | None = None,
    omit: str | None = None,
    conflict: str = "",
) -> int:
    source_columns = _columns(conn, "source", table)
    if not source_columns:
        return 0
    replacements = override or {}
    columns = [
        column
        for column in _columns(conn, "main", table)
        if (column in source_columns or column in replacements)
        and (only is None or column in only)
        and column != omit
    ]
    identifiers = [_identifier(column) for column in columns]
    values = [
        replacements.get(column, f"old.{identifier}")
        for column, identifier in zip(columns, identifiers, strict=True)
    ]
    # Table names and SQL fragments come from the calls below. Column names come
    # from the destination schema and are always quoted as SQLite identifiers.
    return conn.execute(
        """INSERT INTO main.{table} ({columns})
           SELECT {values} FROM source.{table} AS old
           {selection}
           {conflict}""".format(
            table=_identifier(table),
            columns=", ".join(identifiers),
            values=", ".join(values),
            selection=selection,
            conflict=conflict,
        )
    ).rowcount


def _copy_paid_records(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    conflict: str,
    *,
    importing: bool,
) -> int:
    inserted = _copy(
        conn,
        table,
        "WHERE true",
        omit=id_column if importing else None,
        conflict=conflict,
    )
    source_columns = _columns(conn, "source", table)
    if not source_columns:
        return inserted
    columns = ", ".join(
        _identifier(column)
        for column in _columns(conn, "main", table)
        if column in source_columns and column != id_column
    )
    # Compare payloads after inserting new keys. EXCEPT also compares nullable
    # columns and permits retries even when the destination assigned other IDs.
    difference = conn.execute(
        """SELECT {columns} FROM source.{table}
           EXCEPT SELECT {columns} FROM main.{table}
           LIMIT 1""".format(columns=columns, table=_identifier(table))
    ).fetchone()
    if difference is not None:
        raise TransferError(
            f"Conflicting records in {table}. Resolve the paid records with the same key and retry."
        )
    return inserted


def _select_sessions(conn: sqlite3.Connection, *, importing: bool) -> None:
    source_columns = _columns(conn, "source", "sessions")
    if not source_columns:
        raise TransferError("The source does not contain an Insights sessions table.")
    conn.execute(
        """CREATE TEMP TABLE transfer_sessions (
               session_id TEXT PRIMARY KEY,
               copy_dependents INTEGER NOT NULL
           )"""
    )
    if importing:
        conn.execute(
            """INSERT INTO transfer_sessions
               SELECT old.session_id, 1 FROM source.sessions AS old
               WHERE NOT EXISTS (SELECT 1 FROM main.sessions WHERE session_id = old.session_id)"""
        )
        return
    if "origin" in source_columns:
        conn.execute(
            """INSERT INTO transfer_sessions
               SELECT session_id, 1 FROM source.sessions WHERE origin = 'legacy'"""
        )
    if _columns(conn, "source", "llm_analyses"):
        conn.execute(
            """INSERT INTO transfer_sessions
               SELECT DISTINCT session_id, 0 FROM source.llm_analyses WHERE true
               ON CONFLICT (session_id) DO NOTHING"""
        )


def _copy_references(conn: sqlite3.Connection, *, importing: bool) -> None:
    has_paths = importing and bool(_columns(conn, "source", "project_paths"))
    conn.execute(
        """CREATE TEMP VIEW transfer_projects AS
           SELECT DISTINCT project_id FROM source.sessions JOIN transfer_sessions USING (session_id)
           WHERE project_id IS NOT NULL"""
    )
    _copy(
        conn,
        "machines",
        """WHERE old.machine_id IN (
               SELECT machine_id FROM source.sessions JOIN transfer_sessions USING (session_id)
           )""",
        conflict="ON CONFLICT (machine_id) DO NOTHING",
    )
    _copy(
        conn,
        "projects",
        "JOIN transfer_projects USING (project_id) WHERE true",
        omit="project_id" if importing else None,
        conflict="ON CONFLICT (canonical_name) DO NOTHING",
    )
    conn.execute(
        """CREATE TEMP VIEW transfer_project_map AS
           SELECT source.projects.project_id AS previous_id, main.projects.project_id AS project_id
           FROM source.projects JOIN main.projects USING (canonical_name)"""
    )
    if has_paths:
        _copy(
            conn,
            "machines",
            """WHERE old.machine_id IN (
                   SELECT machine_id FROM source.project_paths
                   JOIN transfer_projects USING (project_id)
               )""",
            conflict="ON CONFLICT (machine_id) DO NOTHING",
        )
        _copy(
            conn,
            "project_paths",
            """JOIN transfer_projects USING (project_id)
               JOIN transfer_project_map ON previous_id = old.project_id WHERE true""",
            override={"project_id": "transfer_project_map.project_id"},
            conflict="ON CONFLICT (machine_id, path) DO NOTHING",
        )


def transfer(conn: sqlite3.Connection, source_uri: str, *, importing: bool) -> dict[str, int]:
    """Copy the selected primary rows, leaving the transaction open for verification."""
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.execute("ATTACH DATABASE ? AS source", (source_uri,))
    conn.execute("BEGIN IMMEDIATE")
    _select_sessions(conn, importing=importing)
    _copy_references(conn, importing=importing)
    session_selection = """JOIN transfer_sessions USING (session_id)
                           LEFT JOIN transfer_project_map ON previous_id = old.project_id
                           WHERE copy_dependents = {}"""
    overrides = {"project_id": "transfer_project_map.project_id"}
    if importing:
        overrides["origin"] = "'legacy'"
    counts = {
        "legacy sessions": _copy(conn, "sessions", session_selection.format(1), override=overrides)
    }
    if not importing:
        counts["analysis parent sessions"] = _copy(
            conn,
            "sessions",
            session_selection.format(0),
            override=overrides,
            only=_SESSION_KEYS,
        )
    for table in _DEPENDENTS:
        counts[table] = _copy(
            conn, table, "JOIN transfer_sessions USING (session_id) WHERE copy_dependents = 1"
        )
    counts["llm_analyses"] = _copy_paid_records(
        conn,
        "llm_analyses",
        "analysis_id",
        "ON CONFLICT (session_id, analysis_type, prompt_version) DO NOTHING",
        importing=importing,
    )
    counts["coach_reports"] = _copy_paid_records(
        conn,
        "coach_reports",
        "coach_id",
        "ON CONFLICT (filter_hash, prompt_version) DO NOTHING",
        importing=importing,
    )
    if conn.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise TransferError("The copied rows failed the destination foreign-key check.")
    return counts


def print_counts(counts: dict[str, int]) -> None:
    for label, count in counts.items():
        print(f"{label}: {count}")
