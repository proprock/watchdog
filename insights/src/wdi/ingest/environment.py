"""Refresh environment and checkout metadata from a Watchdog store."""

from agent_watchdog import readapi
from wdi.db.repository import Repository


def import_environment(repo: Repository, store: readapi.StoreInfo) -> None:
    if not store.supported or store.machine_id is None or store.project_id is None:
        raise ValueError(f"Unsupported or incomplete store: {store.path}")
    machine, project, path = store.machine_id, store.project_id, str(store.path.resolve())
    with repo.import_transaction():
        repo.conn.execute(
            "DELETE FROM session_env WHERE machine_id = ? AND project_id = ?", (machine, project)
        )
        repo.conn.execute(
            "DELETE FROM checkout_states WHERE machine_id = ? AND project_id = ?",
            (machine, project),
        )
        repo.conn.executemany(
            """INSERT INTO session_env VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    machine,
                    project,
                    path,
                    *(
                        row[key]
                        for key in (
                            "provider",
                            "session_id",
                            "captured_at",
                            "cwd",
                            "checkout_id",
                            "head",
                            "branch",
                            "harness",
                            "files",
                            "watchdog",
                            "initial_diff_digest",
                            "initial_diff_error",
                            "initial_files_changed",
                            "initial_insertions",
                            "initial_deletions",
                            "initial_untracked",
                        )
                    ),
                )
                for row in readapi.session_env(store)
            ),
        )
        repo.conn.executemany(
            """INSERT INTO checkout_states VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    machine,
                    project,
                    path,
                    *(
                        row[key]
                        for key in (
                            "checkout_id",
                            "observed_at",
                            "provider",
                            "session_id",
                            "trigger",
                            "base_head",
                            "head",
                            "branch",
                            "files_changed",
                            "insertions",
                            "deletions",
                            "untracked",
                            "diff_digest",
                            "error",
                        )
                    ),
                )
                for row in readapi.checkout_states(store)
            ),
        )
