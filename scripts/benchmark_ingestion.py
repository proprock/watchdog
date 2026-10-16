"""Opt-in synthetic spool-to-SQLite benchmark; no provider or network calls."""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import sqlite3
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from agent_watchdog import resources
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.daemon import _drain_spool
from agent_watchdog.registry import Registry, discover
from agent_watchdog.storage import Inbox, Store


def _project_id(source_db: Path) -> UUID:
    with closing(sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT project_id FROM metadata").fetchall()
    if len(rows) != 1:
        raise ValueError("Source database has no unique project identity")
    return UUID(rows[0][0])


def _backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)


def _record(checkout: Path, index: int) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "received_at": now,
        "provider": "codex",
        "cwd": str(checkout),
        "input": {
            "hook_event_name": "PostToolUse",
            "session_id": "benchmark-session",
            "tool_name": "Bash",
            "tool_input": {"command": f"printf benchmark-{index}"},
            "tool_response": {"stdout": "synthetic", "exit_code": 0},
        },
        "delivery": {},
    }


def _timed[**P, R](target: Callable[P, R], label: str, costs: dict[str, float]) -> Callable[P, R]:
    @wraps(target)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        try:
            return target(*args, **kwargs)
        finally:
            costs[label] = costs.get(label, 0.0) + time.perf_counter() - started

    return invoke


def _profile_stages(profiles: list[tuple[str, cProfile.Profile]]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    needles = {
        "fact_projection": ("storage.py", "_project_fact"),
        "session_state_apply": ("storage.py", "_apply_state"),
        "session_state_flush": ("storage.py", "_flush_codex_state"),
        "session_replay": ("storage.py", "_replay_codex_state"),
    }
    for phase, profile in profiles:
        stats = pstats.Stats(profile)
        raw_stats = getattr(stats, "stats", {})
        read_seconds = 0.0
        read_seen = False
        for (filename, _line, function), (_cc, _nc, tt, _ct, _callers) in raw_stats.items():
            for label, (file_part, function_part) in needles.items():
                if file_part in filename and function_part == function:
                    values[f"{label}_exclusive"] = (values.get(f"{label}_exclusive") or 0.0) + tt
                    values[f"{label}_inclusive"] = (values.get(f"{label}_inclusive") or 0.0) + _ct
            if "<method 'read'" in function or "<built-in method read" in function:
                read_seconds += tt
                read_seen = True
        values[f"file_read_calls_{phase}_exclusive"] = read_seconds if read_seen else None
    return values


def _run_copy(
    source_db: Path,
    checkout: Path,
    parent: Path,
    events: int,
    batch_size: int,
    *,
    profile_stages: bool,
) -> dict:
    project_id = _project_id(source_db)
    data = parent / "state"
    project_root = checkout.resolve()
    project = Project(
        id=project_id,
        root=project_root,
        git_common_dir=discover(project_root).git_common_dir,
    )
    config = Config(projects=(project,))
    paths = UserPaths(parent / "config.toml", data, parent / "runtime")
    save_config(paths.config, config)
    root = paths.project_data(project_id)
    _backup(source_db, root / "events.sqlite3")
    spool = data / "spool"
    spool.mkdir(parents=True)
    for index in range(events):
        (spool / f"{index:08d}.json").write_text(
            json.dumps(_record(project_root, index)), encoding="utf-8"
        )
    inbox = Inbox(root)
    costs: dict[str, float] = {}
    commit_calls: list[float] = []
    insert_calls: list[float] = []
    profiles: list[tuple[str, cProfile.Profile]] = []
    patches = ExitStack()
    if profile_stages:
        registry_resolve = Registry.resolve
        publish = Inbox.publish
        store_put = Store.put
        import agent_watchdog.daemon as daemon
        import agent_watchdog.hooks as hooks

        capture_fn = daemon._capture_diff_snapshot
        build_envelope = hooks.build_envelope
        patches.enter_context(
            patch.object(
                Registry, "resolve", _timed(registry_resolve, "registry_resolution", costs)
            )
        )
        patches.enter_context(
            patch.object(Inbox, "publish", _timed(publish, "inbox_publication", costs))
        )
        patches.enter_context(patch.object(Store, "put", _timed(store_put, "store_put", costs)))
        patches.enter_context(
            patch.object(
                daemon,
                "_capture_diff_snapshot",
                _timed(capture_fn, "checkout_fingerprint", costs),
            )
        )
        patches.enter_context(
            patch.object(hooks, "build_envelope", _timed(build_envelope, "envelope_build", costs))
        )
        original_connect = sqlite3.connect

        class ProfiledConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                started_at = time.perf_counter()
                try:
                    return super().execute(sql, parameters)
                finally:
                    elapsed = time.perf_counter() - started_at
                    statement = sql.strip().upper()
                    if statement == "COMMIT":
                        commit_calls.append(elapsed)
                    elif statement.startswith("INSERT"):
                        insert_calls.append(elapsed)

        def profiled_connect(*args, **kwargs):
            kwargs["factory"] = ProfiledConnection
            return original_connect(*args, **kwargs)

        patches.enter_context(patch.object(sqlite3, "connect", profiled_connect))
        patches.enter_context(
            patch.object(
                resources,
                "available",
                _timed(resources.available, "quota_availability_scan", costs),
            )
        )
    try:
        spool_profile = cProfile.Profile() if profile_stages else None
        inbox_profile = cProfile.Profile() if profile_stages else None
        spool_seconds = inbox_seconds = 0.0
        inserted = duplicates = 0
        # Match the daemon's per-project reader windows. Staging the entire
        # backlog first artificially inflates every publisher's quota scan.
        while inserted + duplicates < events:
            if spool_profile is not None:
                spool_profile.enable()
            started = time.perf_counter()
            activity = _drain_spool(paths, config, limit=100)
            spool_seconds += time.perf_counter() - started
            if spool_profile is not None:
                spool_profile.disable()
            if inbox_profile is not None:
                inbox_profile.enable()
            started = time.perf_counter()
            with Store(root, project_id) as store:
                drained = inbox.drain(store, batch_size=batch_size)
            inbox_seconds += time.perf_counter() - started
            if inbox_profile is not None:
                inbox_profile.disable()
            inserted += drained.inserted
            duplicates += drained.duplicates
            if not activity and drained.inserted + drained.duplicates == 0:
                raise RuntimeError("Ingestion stalled before every synthetic event was stored")
        if spool_profile is not None and inbox_profile is not None:
            profiles.extend((("spool_phase", spool_profile), ("inbox_phase", inbox_profile)))
    finally:
        if profile_stages:
            patches.close()
    if profile_stages:
        stage_profile = _profile_stages(profiles)
        stage_profile.update(costs)
        stage_profile["sqlite_commit_including_sync"] = sum(commit_calls)
        stage_profile["sqlite_insert_execution"] = sum(insert_calls)
    else:
        stage_profile = None
    return {
        "batch_size": batch_size,
        "events_requested": events,
        "events_inserted": inserted,
        "duplicates": duplicates,
        "spool_seconds": spool_seconds,
        "inbox_drain_seconds": inbox_seconds,
        "total_seconds": spool_seconds + inbox_seconds,
        "events_per_minute": events / (spool_seconds + inbox_seconds) * 60,
        "stage_profile_seconds": stage_profile,
    }


def run_benchmark(
    source_db: Path,
    checkout: Path,
    work_dir: Path,
    events: int,
    profile_events: int | None = None,
) -> dict:
    if events <= 0:
        raise ValueError("Event count must be positive")
    profile_events = min(events, 200) if profile_events is None else profile_events
    if profile_events <= 0 or profile_events > events:
        raise ValueError("Profile event count must be between one and the workload size")
    source_db = source_db.resolve(strict=True)
    checkout = checkout.resolve(strict=True)
    if source_db.name != "events.sqlite3" or not checkout.is_dir():
        raise ValueError("Expected an events.sqlite3 source and an existing checkout")
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seed-", dir=work_dir) as seed_directory:
        seed = Path(seed_directory) / "source.sqlite3"
        _backup(source_db, seed)
        seed_hash = _sha256(seed)
        seed_size = seed.stat().st_size
        with closing(sqlite3.connect(f"file:{seed.as_posix()}?mode=ro", uri=True)) as db:
            seed_event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        results = []
        for label, batch_size in (("baseline", 1), ("batch", 25)):
            runs = {}
            with tempfile.TemporaryDirectory(prefix=f"{label}-", dir=work_dir) as temporary:
                runs["throughput"] = _run_copy(
                    seed,
                    checkout,
                    Path(temporary) / "throughput",
                    events,
                    batch_size,
                    profile_stages=False,
                )
                runs["stage_profile"] = _run_copy(
                    seed,
                    checkout,
                    Path(temporary) / "profile",
                    profile_events,
                    batch_size,
                    profile_stages=True,
                )
            if runs["throughput"]["events_inserted"] != events:
                raise RuntimeError("Synthetic throughput run did not insert every event")
            if runs["stage_profile"]["events_inserted"] != profile_events:
                raise RuntimeError("Synthetic profiling run did not insert every event")
            throughput = runs["throughput"]
            results.append(
                {
                    "batch_size": batch_size,
                    "events_requested": events,
                    "events_inserted": events,
                    "events_profiled": profile_events,
                    "duplicates": throughput["duplicates"],
                    "spool_seconds": throughput["spool_seconds"],
                    "inbox_seconds": throughput["inbox_drain_seconds"],
                    "total_seconds": throughput["total_seconds"],
                    "events_per_minute": throughput["events_per_minute"],
                    "profile_spool_seconds": runs["stage_profile"]["spool_seconds"],
                    "profile_inbox_seconds": runs["stage_profile"]["inbox_drain_seconds"],
                    "profile_total_seconds": runs["stage_profile"]["total_seconds"],
                    "stage_profile": runs["stage_profile"]["stage_profile_seconds"],
                }
            )
    return {
        "benchmark": "synthetic_spool_to_sqlite",
        "events": events,
        "reader_window_limit": 100,
        "time_budget_seconds": None,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "seed_database_sha256": seed_hash,
        "seed_database_size_bytes": seed_size,
        "seed_event_count": seed_event_count,
        "profiling_note": (
            "Throughput is unprofiled. Stage timing uses separate identical copies; "
            "COMMIT timings include SQLite sync work. File read calls are measured per phase, "
            "including ancillary reads in that phase."
        ),
        "results": results,
    }


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db", type=Path, required=True, help="Explicit source events.sqlite3"
    )
    parser.add_argument("--checkout", type=Path, required=True, help="Registered project checkout")
    parser.add_argument("--work-dir", type=Path, default=Path("tests/local/wd172"))
    parser.add_argument("--events", type=int, default=2000)
    parser.add_argument(
        "--profile-events",
        type=int,
        help="Instrument this many records per batch size (default: min(--events, 200))",
    )
    args = parser.parse_args()
    try:
        report = run_benchmark(
            args.source_db,
            args.checkout,
            args.work_dir,
            args.events,
            args.profile_events,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        parser.error("Benchmark failed; source paths and data are withheld from output")
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
