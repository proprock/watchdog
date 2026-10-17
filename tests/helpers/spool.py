"""Storage-test harness; production has only the daemon's queue consumer."""

import json
import time
from itertools import islice

from pydantic import ValidationError

from agent_watchdog.spool import Spool
from agent_watchdog.storage import DrainResult, RejectedEvent, StorageError, persisted_envelope


class PreparedQueue(Spool):
    def drain(self, store, *, limit=100, batch_size=25, time_budget=None):
        if (
            min(limit, batch_size) <= 0
            or (time_budget is not None and time_budget <= 0)
            or store.connection.in_transaction
        ):
            raise StorageError("Invalid batch bounds")
        result = DrainResult()
        deadline = time.monotonic() + time_budget if time_budget else None
        records = self.records(result, limit=limit)
        while chunk := list(islice(records, batch_size)):
            pending = []
            for path, record in chunk:
                try:
                    if record["record_type"] != "envelope":
                        raise RejectedEvent("Hook requires checkout resolution")
                    event = persisted_envelope(json.dumps(record["envelope"]))
                    if event.project_id != store.project_id:
                        raise RejectedEvent("Event belongs to another project")
                    pending.append((path, event))
                except (KeyError, ValidationError, RejectedEvent):
                    self.quarantine(path, result)
            self.commit(store, pending, result)
            if deadline is not None and time.monotonic() >= deadline:
                break
        return result
