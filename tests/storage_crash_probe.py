"""Crash only this test-owned process at a real SQLite/inbox boundary."""

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agent_watchdog.storage import Inbox, Store


def main() -> None:
    root, project, phase = sys.argv[1:]

    def crash_at_commit(sql: str) -> None:
        if sql == "COMMIT":
            os._exit(91)

    if phase == "migration":
        original_connect = sqlite3.connect

        def connect_with_crash(*args, **kwargs) -> sqlite3.Connection:
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(crash_at_commit)
            return connection

        # The patched process terminates at the migration's COMMIT boundary.
        patch("sqlite3.connect", side_effect=connect_with_crash).start()

    with Store(Path(root), UUID(project)) as store:
        if phase == "before":
            store.connection.set_trace_callback(crash_at_commit)
        elif phase == "after":
            original = Path.unlink

            def crash_at_ack(self: Path, missing_ok: bool = False) -> None:
                if self.parent.name == "inbox" and self.suffix == ".json":
                    os._exit(91)
                original(self, missing_ok=missing_ok)

            Path.unlink = crash_at_ack
        Inbox(Path(root)).drain(store)
    raise RuntimeError("Expected crash boundary was not reached")


if __name__ == "__main__":
    main()
