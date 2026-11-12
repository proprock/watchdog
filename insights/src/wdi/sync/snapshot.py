"""Export the DB as a snapshot and merge foreign snapshots back in.

Keys are stable across machines (session_id/agent_run_id are UUIDs, projects
merge by canonical_name), so merging is idempotent: INSERT OR IGNORE dedupes
re-imported rows. Project ids are local autoincrements, so foreign session rows
have their project_id translated via canonical_name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from wdi.db.connection import connect

# Content tables whose primary key is already globally unique: a plain
# INSERT OR IGNORE ... SELECT * dedupes correctly across machines.
_PASSTHROUGH_TABLES = (
    "messages",
    "tool_calls",
    "context_events",
    "subagent_runs",
)

_SESSION_COLUMNS = (
    "session_id, machine_id, project_id, agent, source_path, cwd, git_branch, "
    "cli_version, originator, title, model, started_at, ended_at, duration_ms, "
    "user_msg_count, assistant_msg_count, tool_call_count, input_tokens, output_tokens, "
    "cache_read_tokens, cache_creation_tokens, reasoning_tokens, context_window, "
    "compaction_count, api_error_count, extra"
)

# analysis_id is a local autoincrement, so it is dropped on merge and dedup
# relies on the UNIQUE(session_id, analysis_type, prompt_version) constraint.
_ANALYSIS_COLUMNS = (
    "session_id, machine_id, analysis_type, prompt_version, model, created_at, score, result"
)


@dataclass(slots=True)
class MergeStats:
    snapshots: int = 0
    machines: int = 0
    projects: int = 0
    sessions: int = 0
    messages: int = 0
    tool_calls: int = 0
    context_events: int = 0
    subagent_runs: int = 0
    project_paths: int = 0
    llm_analyses: int = 0
    skipped: list[str] = field(default_factory=list)


def export_snapshot(db_path: Path, out_dir: Path, machine_id: str) -> Path:
    """Write a clean single-file snapshot named wdi-<machine_id>.sqlite."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"wdi-{machine_id}.sqlite"
    source = connect(db_path)
    dest = sqlite3.connect(out_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return out_path


def _inserted(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT changes()").fetchone()[0]


def _merge_one(conn: sqlite3.Connection, snapshot: Path, stats: MergeStats) -> None:
    conn.execute("ATTACH DATABASE ? AS src", (str(snapshot),))
    try:
        has_origin = conn.execute(
            "SELECT 1 FROM pragma_table_info('sessions', 'src') WHERE name = 'origin'"
        ).fetchone()
        origin = "s.origin" if has_origin else "'file'"
        with conn:
            conn.execute("INSERT OR IGNORE INTO machines SELECT * FROM src.machines")
            stats.machines += _inserted(conn)
            conn.execute(
                "INSERT OR IGNORE INTO projects (canonical_name) "
                "SELECT canonical_name FROM src.projects"
            )
            stats.projects += _inserted(conn)
            conn.execute(
                """CREATE TEMP TABLE snapshot_existing_legacy AS
                   SELECT session_id FROM main.sessions WHERE origin = 'legacy'"""
            )
            # Translate foreign project_id -> local project_id by canonical_name.
            conn.execute(
                f"""
                INSERT OR IGNORE INTO sessions ({_SESSION_COLUMNS}, origin)
                SELECT s.session_id, s.machine_id,
                    (SELECT lp.project_id FROM projects lp
                     JOIN src.projects sp ON sp.canonical_name = lp.canonical_name
                     WHERE sp.project_id = s.project_id),
                    s.agent, s.source_path, s.cwd, s.git_branch, s.cli_version, s.originator,
                    s.title, s.model, s.started_at, s.ended_at, s.duration_ms, s.user_msg_count,
                    s.assistant_msg_count, s.tool_call_count, s.input_tokens, s.output_tokens,
                    s.cache_read_tokens, s.cache_creation_tokens, s.reasoning_tokens,
                    s.context_window, s.compaction_count, s.api_error_count, s.extra, {origin}
                FROM src.sessions s
                """  # noqa: S608 - fixed column list, no user input
            )
            stats.sessions += _inserted(conn)
            copy_stream_rows = """INSERT OR IGNORE INTO main.{table}
                                  SELECT * FROM src.{table}
                                  WHERE session_id NOT IN (
                                      SELECT session_id FROM snapshot_existing_legacy
                                  )"""
            for table in _PASSTHROUGH_TABLES:
                conn.execute(copy_stream_rows.format(table=table))
                setattr(stats, table, getattr(stats, table) + _inserted(conn))
            conn.execute("DROP TABLE snapshot_existing_legacy")
            conn.execute("INSERT OR IGNORE INTO project_paths SELECT * FROM src.project_paths")
            stats.project_paths += _inserted(conn)
            conn.execute(
                f"INSERT OR IGNORE INTO llm_analyses ({_ANALYSIS_COLUMNS}) "  # noqa: S608
                f"SELECT {_ANALYSIS_COLUMNS} FROM src.llm_analyses"
            )
            stats.llm_analyses += _inserted(conn)
    finally:
        conn.execute("DETACH DATABASE src")


def import_snapshots(db_path: Path, in_dir: Path, self_machine_id: str) -> MergeStats:
    """Merge every wdi-*.sqlite in in_dir (except this machine's own) into the DB."""
    stats = MergeStats()
    conn = connect(db_path)
    try:
        own = f"wdi-{self_machine_id}.sqlite"
        for snapshot in sorted(in_dir.glob("wdi-*.sqlite")):
            if snapshot.name == own:
                continue
            try:
                _merge_one(conn, snapshot, stats)
                stats.snapshots += 1
            except sqlite3.Error as exc:
                stats.skipped.append(f"{snapshot.name}: {exc}")
    finally:
        conn.close()
    return stats
