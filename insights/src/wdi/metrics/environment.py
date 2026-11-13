"""Shape captured session environment and resolve captured diffs on demand."""

import json
import sqlite3
from pathlib import Path

from agent_watchdog import readapi
from wdi.metrics.filters import environment_source_condition, instructions_changed_sql


def _environment(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        f"""SELECT e.* FROM sessions s JOIN session_env e
        ON e.session_id = s.session_id AND e.machine_id = s.machine_id AND e.provider = s.agent
        WHERE s.session_id = ? AND {environment_source_condition("s", "e")}
        ORDER BY e.captured_at DESC, e.project_id, e.store_path LIMIT 1""",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def _outcome(conn: sqlite3.Connection, env: dict | None) -> dict | None:
    if env is None or env["checkout_id"] is None:
        return None
    row = conn.execute(
        """SELECT * FROM checkout_states WHERE machine_id = ? AND project_id = ?
        AND store_path = ? AND checkout_id = ? AND provider = ? AND session_id = ?
        ORDER BY observed_at DESC LIMIT 1""",
        tuple(
            env[key]
            for key in (
                "machine_id",
                "project_id",
                "store_path",
                "checkout_id",
                "provider",
                "session_id",
            )
        ),
    ).fetchone()
    return dict(row) if row else None


def _initial(conn: sqlite3.Connection, env: dict | None) -> dict | None:
    if env is None:
        return None
    row = conn.execute(
        """SELECT * FROM session_env WHERE machine_id = ? AND project_id = ?
           AND store_path = ? AND provider = ? AND session_id = ?
           ORDER BY captured_at LIMIT 1""",
        tuple(
            env[key] for key in ("machine_id", "project_id", "store_path", "provider", "session_id")
        ),
    ).fetchone()
    return dict(row) if row else None


def _previous_environment(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        """SELECT p.session_id FROM sessions s JOIN sessions p
        ON p.project_id = s.project_id AND p.started_at < s.started_at
        WHERE s.session_id = ? ORDER BY p.started_at DESC, p.session_id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return _environment(conn, row[0]) if row else None


def session_panels(conn: sqlite3.Connection, session_id: str) -> dict:
    env = _environment(conn, session_id)
    outcome = _outcome(conn, env)
    previous = _previous_environment(conn, session_id)
    files = []
    rules = []
    if env is not None:
        current_files = json.loads(env["files"])
        previous_files = json.loads(previous["files"]) if previous else None
        old = {(item["path"], item["role"]): item for item in previous_files or []}
        new = {(item["path"], item["role"]): item for item in current_files}
        for key in dict.fromkeys([*new, *old]):
            current, prior = new.get(key), old.get(key)
            item = dict(current if current is not None else old[key])
            if previous_files is None:
                changed = None
            elif current is None or prior is None:
                changed = True
            elif (
                current.get("digest") is None
                or prior.get("digest") is None
                or current.get("error")
                or prior.get("error")
            ):
                changed = None
            else:
                changed = current["digest"] != prior["digest"]
            item["changed"] = changed
            files.append(item)
        env["harness"] = json.loads(env["harness"])
        env["watchdog"] = json.loads(env["watchdog"])
        rules = env["watchdog"].get("rules", [])
    row = conn.execute(
        "SELECT " + instructions_changed_sql("s") + " FROM sessions s WHERE s.session_id = ?",
        (session_id,),
    ).fetchone()
    changed = None if row is None or row[0] is None else bool(row[0])
    head_moved = None
    if outcome and outcome["base_head"] is not None and outcome["head"] is not None:
        head_moved = outcome["base_head"] != outcome["head"]
    return {
        "environment": env,
        "initial": _initial(conn, env),
        "outcome": outcome,
        "files": files,
        "rules": rules,
        "instructions_changed": changed,
        "head_moved": head_moved,
    }


def read_session_diff(
    conn: sqlite3.Connection, session_id: str, kind: str, data_dirs: list[Path]
) -> dict:
    env = _environment(conn, session_id)
    if kind not in ("initial", "outcome"):
        return {"text": None, "reason": "Unknown diff type."}
    snapshot = _initial(conn, env) if kind == "initial" else _outcome(conn, env)
    if snapshot is None:
        return {"text": None, "reason": "Capture is unavailable."}
    digest = snapshot["initial_diff_digest" if kind == "initial" else "diff_digest"]
    if not digest:
        return {"text": None, "reason": "No captured diff is available."}
    try:
        for directory in data_dirs:
            for info in readapi.stores(Path(directory)):
                if (
                    info.machine_id == snapshot["machine_id"]
                    and info.project_id == snapshot["project_id"]
                    and str(info.path.resolve()) == snapshot["store_path"]
                ):
                    return {
                        "text": readapi.blob_bytes(info, digest).decode("utf-8", errors="replace"),
                        "reason": None,
                    }
    except OSError, ValueError, KeyError, sqlite3.Error:
        return {"text": None, "reason": "Captured diff is unavailable."}
    return {"text": None, "reason": "Capture store is unavailable."}
