"""WD-158: reporting indexes are installed on existing stores and used by real reads."""

import sqlite3
from uuid import uuid4

from agent_watchdog import facts_query
from agent_watchdog.analysis import RULE_VERSION
from agent_watchdog.storage import PENDING_SESSIONS, Store


def test_reporting_reads_use_indexes_after_reopening_an_existing_store(tmp_path):
    project = uuid4()
    with Store(tmp_path, project):
        pass
    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        db.execute("DROP INDEX IF EXISTS events_provider_session")
        db.execute("DROP INDEX IF EXISTS event_facts_usage_identity")

    with Store(tmp_path, project) as store:
        db = store.connection
        statements = []
        db.set_trace_callback(statements.append)
        facts_query.token_usage(db)
        db.set_trace_callback(None)
        token_sql = next(sql for sql in statements if sql.startswith("SELECT"))
        plan = " ".join(str(row) for row in db.execute("EXPLAIN QUERY PLAN " + token_sql))
        assert "event_facts_usage_identity" in plan
        for sql, params in (
            (PENDING_SESSIONS, (RULE_VERSION,)),
            (
                "SELECT COUNT(*) FROM (SELECT provider, session_id FROM events "
                "WHERE session_id IS NOT NULL GROUP BY 1, 2)",
                (),
            ),
        ):
            plan = " ".join(str(row) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))
            assert "COVERING INDEX events_provider_session" in plan
