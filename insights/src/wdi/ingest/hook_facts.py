"""Import read-contract hook records and their shared incremental cursor."""

from itertools import islice, takewhile

from agent_watchdog import readapi
from wdi.db.repository import Repository


def import_hook_facts(
    repo: Repository, stores: list[readapi.StoreInfo], *, full: bool = False
) -> int:
    """Import each store atomically; return the number of source events read."""
    count = 0
    for store in stores:
        if not store.supported or store.machine_id is None or store.project_id is None:
            raise ValueError(f"Unsupported or incomplete store: {store.path}")
        machine, project = store.machine_id, store.project_id
        key = f"wd-facts://{machine}/{project}"
        state = repo.get_import_state(machine, key)
        cursor = 0 if full or state is None else state.byte_offset
        repo.ensure_machine(machine, hostname=store.hostname, os_name=store.os)
        end = cursor
        imported = 0
        with repo.import_transaction():
            if full:
                repo.conn.execute(
                    "DELETE FROM hook_events WHERE machine_id = ? AND project_id = ?",
                    (machine, project),
                )
            event_columns = (
                "event_rowid",
                "event_id",
                "received_at",
                "session_id",
                "kind",
                "envelope",
            )
            event_rows = iter(readapi.events(store, after_rowid=cursor))
            while batch := list(islice(event_rows, 500)):
                repo.conn.executemany(
                    "INSERT INTO hook_events (machine_id, project_id, "
                    + ", ".join(event_columns)
                    + ") VALUES ("
                    + ", ".join("?" for _ in range(len(event_columns) + 2))
                    + ")",
                    [
                        (machine, project, *(row[column] for column in event_columns))
                        for row in batch
                    ],
                )
                end = batch[-1]["event_rowid"]
                imported += len(batch)
            fact_columns = ("event_rowid", *readapi.FACT_COLUMNS)
            facts = takewhile(
                lambda row, limit=end: row["event_rowid"] <= limit,
                readapi.event_facts(store, after_rowid=cursor),
            )
            while batch := list(islice(facts, 500)):
                repo.conn.executemany(
                    "INSERT INTO hook_facts (machine_id, project_id, "
                    + ", ".join(fact_columns)
                    + ") VALUES ("
                    + ", ".join("?" for _ in range(len(fact_columns) + 2))
                    + ")",
                    [
                        (machine, project, *(row[column] for column in fact_columns))
                        for row in batch
                    ],
                )
            if imported or full:
                repo.set_import_state(machine, key, file_size=0, file_mtime=0, byte_offset=end)
        count += imported
    return count
